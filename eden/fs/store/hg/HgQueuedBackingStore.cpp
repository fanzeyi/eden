/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#include "eden/fs/store/hg/HgQueuedBackingStore.h"

#include <folly/Range.h>
#include <folly/futures/Future.h>
#include <folly/logging/xlog.h>
#include <gflags/gflags.h>
#include <thread>
#include <utility>
#include <variant>

#include "eden/fs/config/ReloadableConfig.h"
#include "eden/fs/model/Blob.h"
#include "eden/fs/store/LocalStore.h"
#include "eden/fs/store/hg/HgBackingStore.h"
#include "eden/fs/store/hg/HgImportRequest.h"
#include "eden/fs/store/hg/HgProxyHash.h"
#include "eden/fs/telemetry/EdenStats.h"
#include "eden/fs/telemetry/RequestMetricsScope.h"
#include "eden/fs/utils/Bug.h"
#include "eden/fs/utils/EnumValue.h"

namespace facebook {
namespace eden {

DEFINE_uint64(hg_queue_batch_size, 1, "Number of requests per Hg import batch");

HgQueuedBackingStore::HgQueuedBackingStore(
    std::shared_ptr<LocalStore> localStore,
    std::shared_ptr<EdenStats> stats,
    std::unique_ptr<HgBackingStore> backingStore,
    uint8_t numberThreads)
    : localStore_(std::move(localStore)),
      stats_(std::move(stats)),
      backingStore_(std::move(backingStore)) {
  threads_.reserve(numberThreads);
  for (int i = 0; i < numberThreads; i++) {
    threads_.emplace_back(&HgQueuedBackingStore::processRequest, this);
  }
}

HgQueuedBackingStore::~HgQueuedBackingStore() {
  queue_.stop();
  for (auto& thread : threads_) {
    thread.join();
  }
}

void HgQueuedBackingStore::processBlobImportRequests(
    std::vector<HgImportRequest>&& requests) {
  std::vector<Hash> hashes;
  folly::stop_watch<std::chrono::milliseconds> watch;
  hashes.reserve(requests.size());

  XLOG(DBG4) << "Processing blob import batch size=" << requests.size();

  for (auto& request : requests) {
    auto& hash = request.getRequest<HgImportRequest::BlobImport>()->hash;
    XLOG(DBG4) << "Processing blob request for " << hash;
    hashes.emplace_back(hash);
  }

  auto proxyHashesTry =
      HgProxyHash::getBatch(localStore_.get(), hashes).wait().getTry();

  if (proxyHashesTry.hasException()) {
    // TODO(zeyi): We should change HgProxyHash::getBatch to make it return
    // partial result instead of fail the entire batch.
    XLOG(WARN) << "Failed to get proxy hash: "
               << proxyHashesTry.exception().what();

    for (auto& request : requests) {
      request.getPromise<HgImportRequest::BlobImport::Response>()->setException(
          proxyHashesTry.exception());
    }

    return;
  }

  auto proxyHashes = proxyHashesTry.value();

  // logic:
  // check with hgcache with the Rust code, if it does not exist there, try
  // hgimporthelper and mononoke if possible

  {
    // check hgcache
    auto request = requests.begin();
    auto proxyHash = proxyHashes.begin();
    auto& stats = stats_->getHgBackingStoreStatsForCurrentThread();
    size_t count = 0;

    XCHECK_EQ(requests.size(), proxyHashes.size());
    for (; request != requests.end();) {
      auto hash = request->getRequest<HgImportRequest::BlobImport>()->hash;

      if (auto blob = backingStore_->getBlobFromHgCache(hash, *proxyHash)) {
        XLOG(DBG4) << "Imported blob from hgcache for " << hash;
        request->getPromise<decltype(blob)>()->setValue(std::move(blob));
        stats.hgBackingStoreGetBlob.addValue(watch.elapsed().count());
        count += 1;

        // Swap-and-pop, removing fulfilled request from the list.
        // It is fine to call `.back()` here because if we are in the loop
        // `requests` are guaranteed to be nonempty.
        // @lint-ignore HOWTOEVEN ParameterUncheckedArrayBounds
        std::swap(*request, requests.back());
        requests.pop_back();

        // Same reason as above, if `proxyHashes` is empty it would be caught
        // by the XHCECK_EQ call above.
        // @lint-ignore HOWTOEVEN LocalUncheckedArrayBounds
        std::swap(*proxyHash, proxyHashes.back());
        proxyHashes.pop_back();
      } else {
        request++;
        proxyHash++;
      }
    }

    XLOG(DBG4) << "Fetched " << count << " requests from hgcache";
  }

  // TODO: check EdenAPI

  {
    auto request = requests.begin();
    auto proxyHash = proxyHashes.begin();
    std::vector<folly::SemiFuture<folly::Unit>> futures;
    futures.reserve(requests.size());

    XCHECK_EQ(requests.size(), proxyHashes.size());
    for (; request != requests.end(); request++, proxyHash++) {
      futures.emplace_back(
          backingStore_->fetchBlobFromHgImporter(*proxyHash)
              .defer([request = std::move(*request), watch, stats = stats_](
                         auto&& result) mutable {
                auto hash =
                    request.getRequest<HgImportRequest::BlobImport>()->hash;
                XLOG(DBG4) << "Imported blob from HgImporter for " << hash;
                stats->getHgBackingStoreStatsForCurrentThread()
                    .hgBackingStoreGetBlob.addValue(watch.elapsed().count());
                request.getPromise<HgImportRequest::BlobImport::Response>()
                    ->setTry(std::forward<decltype(result)>(result));
              }));
    }

    folly::collectAll(futures).wait();
  }
}

void HgQueuedBackingStore::processTreeImportRequests(
    std::vector<HgImportRequest>&& requests) {
  for (auto& request : requests) {
    auto parameter = request.getRequest<HgImportRequest::TreeImport>();
    request.getPromise<HgImportRequest::TreeImport::Response>()->setWith(
        [store = backingStore_.get(), hash = parameter->hash]() {
          return store->getTree(hash).getTry();
        });
  }
}

void HgQueuedBackingStore::processPrefetchRequests(
    std::vector<HgImportRequest>&& requests) {
  for (auto& request : requests) {
    auto parameter = request.getRequest<HgImportRequest::Prefetch>();
    request.getPromise<HgImportRequest::Prefetch::Response>()->setWith(
        [store = backingStore_.get(), hashes = parameter->hashes]() {
          return store->prefetchBlobs(hashes).getTry();
        });
  }
}

void HgQueuedBackingStore::processRequest() {
  for (;;) {
    auto requests = queue_.dequeue(FLAGS_hg_queue_batch_size);

    if (requests.empty()) {
      break;
    }

    const auto& first = requests.at(0);

    if (first.isType<HgImportRequest::BlobImport>()) {
      processBlobImportRequests(std::move(requests));
    } else if (first.isType<HgImportRequest::TreeImport>()) {
      processTreeImportRequests(std::move(requests));
    } else if (first.isType<HgImportRequest::Prefetch>()) {
      processPrefetchRequests(std::move(requests));
    }
  }
}

folly::SemiFuture<std::unique_ptr<Tree>> HgQueuedBackingStore::getTree(
    const Hash& id,
    ImportPriority priority) {
  auto importTracker =
      std::make_unique<RequestMetricsScope>(&pendingImportTreeWatches_);
  auto [request, future] = HgImportRequest::makeTreeImportRequest(
      id, priority, std::move(importTracker));
  queue_.enqueue(std::move(request));
  return std::move(future);
}

folly::SemiFuture<std::unique_ptr<Blob>> HgQueuedBackingStore::getBlob(
    const Hash& id,
    ImportPriority priority) {
  auto proxyHash = HgProxyHash(localStore_.get(), id, "getBlob");
  if (auto blob =
          backingStore_->getDatapackStore().getBlobLocal(id, proxyHash)) {
    return folly::makeSemiFuture(std::move(blob));
  }

  auto importTracker =
      std::make_unique<RequestMetricsScope>(&pendingImportBlobWatches_);
  auto [request, future] = HgImportRequest::makeBlobImportRequest(
      id, priority, std::move(importTracker));
  queue_.enqueue(std::move(request));
  return std::move(future);
}

folly::SemiFuture<std::unique_ptr<Tree>> HgQueuedBackingStore::getTreeForCommit(
    const Hash& commitID) {
  return backingStore_->getTreeForCommit(commitID);
}

folly::SemiFuture<std::unique_ptr<Tree>>
HgQueuedBackingStore::getTreeForManifest(
    const Hash& commitID,
    const Hash& manifestID) {
  return backingStore_->getTreeForManifest(commitID, manifestID);
}

folly::SemiFuture<folly::Unit> HgQueuedBackingStore::prefetchBlobs(
    const std::vector<Hash>& ids) {
  auto importTracker =
      std::make_unique<RequestMetricsScope>(&pendingImportPrefetchWatches_);
  auto [request, future] = HgImportRequest::makePrefetchRequest(
      ids, ImportPriority::kNormal(), std::move(importTracker));
  queue_.enqueue(std::move(request));

  return std::move(future);
}

size_t HgQueuedBackingStore::getImportMetric(
    RequestMetricsScope::RequestStage stage,
    HgBackingStore::HgImportObject object,
    RequestMetricsScope::RequestMetric metric) const {
  return RequestMetricsScope::getMetricFromWatches(
      metric, getImportWatches(stage, object));
}

RequestMetricsScope::LockedRequestWatchList&
HgQueuedBackingStore::getImportWatches(
    RequestMetricsScope::RequestStage stage,
    HgBackingStore::HgImportObject object) const {
  switch (stage) {
    case RequestMetricsScope::RequestStage::PENDING:
      return getPendingImportWatches(object);
    case RequestMetricsScope::RequestStage::LIVE:
      return backingStore_->getLiveImportWatches(object);
  }
  EDEN_BUG() << "unknown hg import stage " << enumValue(stage);
}

RequestMetricsScope::LockedRequestWatchList&
HgQueuedBackingStore::getPendingImportWatches(
    HgBackingStore::HgImportObject object) const {
  switch (object) {
    case HgBackingStore::HgImportObject::BLOB:
      return pendingImportBlobWatches_;
    case HgBackingStore::HgImportObject::TREE:
      return pendingImportTreeWatches_;
    case HgBackingStore::HgImportObject::PREFETCH:
      return pendingImportPrefetchWatches_;
  }
  EDEN_BUG() << "unknown hg import object type " << static_cast<int>(object);
}

} // namespace eden
} // namespace facebook
