/*
 *  Copyright (c) 2016-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#pragma once
#include <folly/File.h>
#include <folly/Synchronized.h>
#include <folly/futures/Future.h>
#include <folly/futures/Promise.h>
#include <stdlib.h>
#include <sys/uio.h>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>

#include "eden/fs/fuse/FuseTypes.h"
#include "eden/fs/utils/PathFuncs.h"

namespace folly {
class RequestContext;
class EventBase;
} // namespace folly

namespace facebook {
namespace eden {
namespace fusell {

class Dispatcher;

class FuseChannel {
 public:
  /**
   * Construct the fuse channel and session structures that are
   * required by libfuse to communicate with the kernel using
   * a pre-existing fuseDevice descriptor.  The descriptor may
   * have been obtained via privilegedFuseMount() or may have
   * been passed to us as part of a graceful restart procedure.
   *
   * The caller is expected to follow up with a call to the
   * initialize() method to perform the handshake with the
   * kernel and set up the thread pool.
   */
  FuseChannel(
      folly::File&& fuseDevice,
      AbsolutePathPiece mountPath,
      folly::EventBase* eventBase,
      size_t numThreads,
      Dispatcher* const dispatcher);

  /**
   * Destroy the FuseChannel.
   *
   * If the FUSE worker threads are still running, the destructor will stop
   * them and wait for them to exit.
   *
   * The destructor must not be invoked from inside one of the worker threads.
   * For instance, do not invoke the destructor from inside a Dispatcher
   * callback.
   */
  ~FuseChannel();

  /**
   * Initialize the FuseChannel; until this completes successfully,
   * FUSE requests will not be serviced.
   *
   * This will first start one worker thread to wait for the INIT request from
   * the kernel and validate that we are compatible.  Once we have successfully
   * completed the INIT negotiation with the kernel we will start the remaining
   * FUSE worker threads and indicate success via the returned Future object.
   *
   * Returns a folly::Future that will complete inside one of the FuseChannel
   * worker threads.
   */
  FOLLY_NODISCARD folly::Future<folly::Unit> initialize();

  /**
   * Initialize the FuseChannel when taking over an existing FuseDevice.
   *
   * This is used when performing a graceful restart of Eden, where we are
   * taking over a FUSE connection that was already initialized by a previous
   * process.
   *
   * The connInfo parameter specifies the connection data that was already
   * negotiated by the previous owner of the FuseDevice.
   *
   * This function will immediately set up the thread pool used to service
   * incoming fuse requests.
   */
  void initializeFromTakeover(fuse_init_out connInfo);

  // Forbidden copy constructor and assignment operator
  FuseChannel(FuseChannel const&) = delete;
  FuseChannel& operator=(FuseChannel const&) = delete;

  /**
   * Request that the FuseChannel stop processing new requests, and prepare
   * to hand over the FuseDevice to another process.
   *
   * TODO: This function should probably return a Future<FuseChannelData>,
   * and we should get rid of the stealFuseDevice() method.
   */
  void takeoverStop() {
    requestSessionExit();
  }

  /**
   * When performing a graceful restart, extract the fuse device
   * descriptor from the channel, preventing it from being closed
   * when we destroy this channel instance.
   * Note that this method does not prevent the worker threads
   * from continuing to use the fuse session.
   */
  FuseChannelData stealFuseDevice();

  /**
   * Notify to invalidate cache for an inode
   *
   * @param ino the inode number
   * @param off the offset in the inode where to start invalidating
   *            or negative to invalidate attributes only
   * @param len the amount of cache to invalidate or 0 for all
   */
  void invalidateInode(fusell::InodeNumber ino, off_t off, off_t len);

  /**
   * Notify to invalidate parent attributes and the dentry matching
   * parent/name
   *
   * @param parent inode number
   * @param name file name
   */
  void invalidateEntry(fusell::InodeNumber parent, PathComponentPiece name);

  /**
   * Sends a reply to a kernel request that consists only of the error
   * status (no additional payload).
   * `err` may be 0 (indicating success) or a positive errno value.
   *
   * throws system_error if the write fails.  Writes can fail if the
   * data we send to the kernel is invalid.
   */
  void replyError(const fuse_in_header& request, int err);

  /**
   * Sends a raw data packet to the kernel.
   * The data may be scattered across a number of discrete buffers;
   * this method uses writev to send them to the kernel as a single unit.
   * The kernel, and thus this method, assumes that the start of this data
   * is a fuse_out_header instance.  This method will sum the iovec lengths
   * to compute the correct value to store into fuse_out_header::len.
   *
   * throws system_error if the write fails.  Writes can fail if the
   * data we send to the kernel is invalid.
   */
  void sendRawReply(const iovec iov[], size_t count) const;

  /**
   * Sends a range of contiguous bytes as a reply to the kernel.
   * request holds the context of the request to which we are replying.
   * `bytes` is the payload to send in addition to the successful status
   * header generated by this method.
   *
   * throws system_error if the write fails.  Writes can fail if the
   * data we send to the kernel is invalid.
   */
  void sendReply(const fuse_in_header& request, folly::ByteRange bytes) const;

  /**
   * Sends a reply to a kernel request, consisting of multiple parts.
   * The `vec` parameter holds an array of payload components and is moved
   * in to this method which then prepends a fuse_out_header and passes
   * control along to sendRawReply().
   *
   * throws system_error if the write fails.  Writes can fail if the
   * data we send to the kernel is invalid.
   */
  void sendReply(const fuse_in_header& request, folly::fbvector<iovec>&& vec)
      const;

  /**
   * Sends a reply to the kernel.
   * The payload parameter is typically a fuse_out_XXX struct as defined
   * in the appropriate fuse_kernel_XXX.h header file.
   *
   * throws system_error if the write fails.  Writes can fail if the
   * data we send to the kernel is invalid.
   */
  template <typename T>
  void sendReply(const fuse_in_header& request, const T& payload) const {
    sendReply(
        request,
        folly::ByteRange{reinterpret_cast<const uint8_t*>(&payload),
                         sizeof(T)});
  }

  /**
   * Called by RequestData when it releases state for the current
   * request.  It is used to update the requests map and is
   * used to trigger the sessionCompletePromise_ if we're
   * shutting down.
   */
  void finishRequest(const fuse_in_header& header);

  /**
   * Returns a Future that will complete when all of the
   * fuse threads have been joined and when all pending
   * fuse requests initiated by the kernel have been
   * responded to.
   *
   * Will throw if called more than once.
   *
   * The session completion future will only be signaled if initialization
   * (via initialize() or takeoverInitialize()) has completed successfully.
   */
  folly::Future<folly::Unit> getSessionCompleteFuture();

 private:
  struct HandlerEntry;
  using HandlerMap = std::unordered_map<uint32_t, HandlerEntry>;

  /**
   * All of our mutable state that may be accessed from the worker threads,
   * and therefore requires synchronization.
   */
  struct State {
    std::unordered_map<uint64_t, std::weak_ptr<folly::RequestContext>> requests;
    std::vector<std::thread> workerThreads;

    /*
     * We track the number of stopped threads, to know when we are done and can
     * signal sessionCompletePromise_.  We only want to signal
     * sessionCompletePromise_ after initialization is successful and then all
     * threads have stopped.
     *
     * If an error occurs during initialization we may have started some but
     * not all of the worker threads.  We do not want to signal
     * sessionCompletePromise_ in this case--we will return the error from
     * initialize() or takeoverInitialize() instead.
     */
    size_t stoppedThreads{0};
  };

  static const HandlerMap handlerMap;

  folly::Future<folly::Unit> fuseRead(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseWrite(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseLookup(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseForget(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseGetAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseSetAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseReadLink(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseSymlink(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseMknod(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseMkdir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseUnlink(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseRmdir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseRename(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseLink(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseOpen(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseStatFs(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseRelease(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseFsync(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseSetXAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseGetXAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseListXAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseRemoveXAttr(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseFlush(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseOpenDir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseReadDir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseReleaseDir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseFsyncDir(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseAccess(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseCreate(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseBmap(
      const fuse_in_header* header,
      const uint8_t* arg);
  folly::Future<folly::Unit> fuseBatchForget(
      const fuse_in_header* header,
      const uint8_t* arg);

  void initWorkerThread() noexcept;
  void fuseWorkerThread() noexcept;
  void maybeDispatchSessionComplete();
  void readInitPacket();
  void startWorkerThreads();

  /**
   * Dispatches fuse requests until the session is torn down.
   * This function blocks until the fuse session is stopped.
   * The intent is that this is called from each of the
   * fuse worker threads provided by the MountPoint.
   */
  void processSession();

  /**
   * Requests that the worker threads terminate their processing loop.
   */
  void requestSessionExit();
  void requestSessionExit(const folly::Synchronized<State>::LockedPtr& state);

  /*
   * Constant state that does not change for the lifetime of the FuseChannel
   */
  const size_t bufferSize_{0};
  const size_t numThreads_;
  Dispatcher* const dispatcher_{nullptr};
  folly::EventBase* const eventBase_;
  const AbsolutePath mountPath_;

  /*
   * connInfo_ is modified during the initialization process,
   * but constant once initialization is complete.
   */
  folly::Optional<fuse_init_out> connInfo_;

  /*
   * TODO: fuseDevice_ needs better synchronization.
   *
   * This is modified by stealFuseDevice().  There currently isn't
   * synchronization enforced between the call to stealFuseDevice() and the
   * destruction of the FuseChannel object, which destroys fuseDevice_ if it
   * has not been modified by stealFuseDevice().
   */
  folly::File fuseDevice_;

  /*
   * Mutable state that is accessed from the worker threads.
   * All of this state uses locking or other synchronization.
   */
  std::atomic<bool> sessionFinished_{false};
  folly::Synchronized<State> state_;
  folly::Promise<folly::Unit> initPromise_;
  folly::Promise<folly::Unit> sessionCompletePromise_;

  // To prevent logging unsupported opcodes twice.
  folly::Synchronized<std::unordered_set<FuseOpcode>> unhandledOpcodes_;
};
} // namespace fusell
} // namespace eden
} // namespace facebook
