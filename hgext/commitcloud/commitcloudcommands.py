# Copyright 2018 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import

# Standard Library
import errno
import itertools
import re
import socket
import time

from mercurial import (
    cmdutil,
    commands,
    error,
    exchange,
    extensions,
    graphmod,
    hg,
    hintutil,
    lock as lockmod,
    node as nodemod,
    obsolete,
    obsutil,
    progress,
    registrar,
    scmutil,
    templatefilters,
    util,
)

# Mercurial
from mercurial.i18n import _

from . import commitcloudcommon, commitcloudutil, service, state


cmdtable = {}
command = registrar.command(cmdtable)
highlightdebug = commitcloudcommon.highlightdebug
highlightstatus = commitcloudcommon.highlightstatus
infinitepush = None
infinitepushbackup = None

# This must match the name from infinitepushbackup in order to maintain
# mutual exclusivity with infinitepushbackups.
_backuplockname = "infinitepushbackup.lock"

workspaceopts = [
    (
        "w",
        "workspace",
        "",
        _("workspace to join (default: 'user/<username>/default') (ADVANCED)"),
    )
]

pullopts = [
    (
        "",
        "direct-fetching",
        None,
        _(
            "try to use directly fetch mercurial bundles instead of pulling through the server "
            "(option requires commitcloud.get_command config) (ADVANCED)"
        ),
    ),
    (
        "",
        "full",
        None,
        _(
            "pull all workspace commits into the local repository, don't omit old ones. (ADVANCED)"
        ),
    ),
]

pushopts = [
    (
        "",
        "push-revs",
        [],
        _(
            "revs to push "
            "(while syncing take into account only the heads built from the given revset) (ADVANCED)"
        ),
        _("REV"),
    )
]


@command("cloud", [], "SUBCOMMAND ...", subonly=True)
def cloud(ui, repo, **opts):
    """synchronise commits via commit cloud

    Commit cloud lets you synchronize commits and bookmarks between
    different copies of the same repository.  This may be useful, for
    example, to keep your laptop and desktop computers in sync.

    Use 'hg cloud join' to connect your repository to the commit cloud
    service and begin synchronizing commits.

    Use 'hg cloud sync' to trigger a new synchronization.  Synchronizations
    also happen automatically in the background as you create and modify
    commits.

    Use 'hg cloud leave' to disconnect your repository from commit cloud.
    """
    pass


subcmd = cloud.subcommand(
    categories=[
        ("Connect to a cloud workspace", ["authenticate", "join"]),
        ("Synchronize with the cloud workspace", ["sync"]),
        ("View other cloud workspaces", ["sl", "ssl"]),
    ]
)


@subcmd("join|connect", [] + workspaceopts + pullopts + pushopts)
def cloudjoin(ui, repo, **opts):
    """connect the local repository to commit cloud

    Commits and bookmarks will be synchronized between all repositories that
    have been connected to the service.

    Use `hg cloud sync` to trigger a new synchronization.
    """

    tokenlocator = commitcloudutil.TokenLocator(ui)
    checkauthenticated(ui, repo, tokenlocator)

    workspacemanager = commitcloudutil.WorkspaceManager(repo)
    if workspacemanager.workspace:
        commitcloudutil.SubscriptionManager(repo).removesubscription()
    workspacemanager.setworkspace(opts.get("workspace"))

    highlightstatus(
        ui,
        _(
            "this repository is now connected to the '%s' "
            "workspace for the '%s' repo\n"
        )
        % (workspacemanager.workspace, workspacemanager.reponame),
    )
    cloudsync(ui, repo, checkbackedup=True, **opts)


@subcmd("rejoin|reconnect", [] + workspaceopts + pullopts + pushopts)
def cloudrejoin(ui, repo, **opts):
    """reconnect the local repository to commit cloud

    Reconnect only happens if the machine has been registered with Commit Cloud,
    and the workspace has been already used for this repo

    Use `hg cloud sync` to trigger a new synchronization.

    Use `hg cloud connect` to connect to commit cloud for the first time.
    """

    educationpage = ui.config("commitcloud", "education_page")
    token = commitcloudutil.TokenLocator(ui).token
    if token:
        try:
            serv = service.get(ui, token)
            serv.check()
            reponame, workspace = getdefaultrepoworkspace(ui, repo)
            if opts.get("workspace"):
                workspace = opts.get("workspace")
            highlightstatus(
                ui,
                _("trying to reconnect to the '%s' workspace for the '%s' repo\n")
                % (workspace, reponame),
            )
            cloudrefs = serv.getreferences(reponame, workspace, 0)
            if cloudrefs.version == 0:
                highlightstatus(
                    ui,
                    _(
                        "unable to reconnect: this workspace has been never connected to Commit Cloud for this repo\n"
                    ),
                )
                if educationpage:
                    ui.status(
                        _("learn more about Commit Cloud at %s\n") % educationpage
                    )
            else:
                commitcloudutil.WorkspaceManager(repo).setworkspace(workspace)
                highlightstatus(ui, _("the repository is now reconnected\n"))
                cloudsync(ui, repo, checkbackedup=True, cloudrefs=cloudrefs, **opts)
            return
        except commitcloudcommon.RegistrationError:
            pass

    highlightstatus(
        ui, _("unable to reconnect: not authenticated with Commit Cloud on this host\n")
    )
    if educationpage:
        ui.status(_("learn more about Commit Cloud at %s\n") % educationpage)


@subcmd("leave|disconnect")
def cloudleave(ui, repo, **opts):
    """disconnect the local repository from commit cloud

    Commits and bookmarks will no londer be synchronized with other
    repositories.
    """
    # do no crash on run cloud leave multiple times
    if not commitcloudutil.getworkspacename(repo):
        highlightstatus(
            ui, _("this repository has been already disconnected from commit cloud\n")
        )
        return
    commitcloudutil.SubscriptionManager(repo).removesubscription()
    commitcloudutil.WorkspaceManager(repo).clearworkspace()
    highlightstatus(ui, _("this repository is now disconnected from commit cloud\n"))


@subcmd("authenticate", [("t", "token", "", _("set or update token"))])
def cloudauth(ui, repo, **opts):
    """authenticate this host with the commit cloud service
    """
    tokenlocator = commitcloudutil.TokenLocator(ui)

    token = opts.get("token")
    if token:
        # The user has provided a token, so just store it.
        if tokenlocator.token:
            ui.status(_("updating authentication token\n"))
        else:
            ui.status(_("setting authentication token\n"))
        # check token actually works
        service.get(ui, token).check()
        tokenlocator.settoken(token)
        ui.status(_("authentication successful\n"))
    else:
        token = tokenlocator.token
        if token:
            try:
                service.get(ui, token).check()
            except commitcloudcommon.RegistrationError:
                token = None
            else:
                ui.status(_("using existing authentication token\n"))
        if token:
            ui.status(_("authentication successful\n"))
        else:
            # Run through interactive authentication
            authenticate(ui, repo, tokenlocator)


@subcmd(
    "smartlog|sl",
    [
        (
            "u",
            "user",
            "",
            _("short username to fetch smartlog view for (default workspace)"),
        )
    ]
    + workspaceopts,
)
def cloudsmartlog(ui, repo, template="sl_cloud", **opts):
    """get smartlog view for the default workspace of the given user

    If the requested template is not defined in the config
    the command provides a simple view as a list of draft commits.
    """

    workspacemanager = commitcloudutil.WorkspaceManager(repo)
    reponame = workspacemanager.reponame
    username = opts.get("user")

    if username:
        workspace = workspacemanager.getdefaultworkspacename(username)
    else:
        if opts.get("workspace"):
            workspace = opts.get("workspace")
        else:
            workspace = workspacemanager.defaultworkspace

    highlightstatus(
        ui,
        _("searching draft commits for the '%s' workspace for the '%s' repo\n")
        % (workspace, reponame),
    )

    serv = service.get(ui, commitcloudutil.TokenLocator(ui).token)

    with progress.spinner(ui, _("fetching")):
        revdag = serv.getsmartlog(reponame, workspace, repo)

    ui.status(_("Smartlog:\n\n"))

    # set up pager
    ui.pager("smartlog")

    smartlogstyle = ui.config("templatealias", template)
    # if style is defined in templatealias section of config apply that style
    if smartlogstyle:
        opts["template"] = "{%s}" % smartlogstyle
    else:
        highlightdebug(ui, _("style %s is not defined, skipping") % smartlogstyle)

    # show all the nodes
    displayer = cmdutil.show_changeset(ui, repo, opts, buffered=True)
    cmdutil.displaygraph(ui, repo, revdag, displayer, graphmod.asciiedges)


@subcmd(
    "supersmartlog|ssl",
    [
        (
            "u",
            "user",
            "",
            _("short username to fetch smartlog view for (default workspace)"),
        )
    ]
    + workspaceopts,
)
def cloudsupersmartlog(ui, repo, **opts):
    """get super smartlog view for the give workspace
    """

    cloudsmartlog(ui, repo, "ssl_cloud", **opts)


def authenticate(ui, repo, tokenlocator):
    """interactive authentication"""
    if not ui.interactive():
        msg = _("authentication with commit cloud required")
        hint = _("use 'hg cloud auth --token TOKEN' to set a token")
        raise commitcloudcommon.RegistrationError(ui, msg, hint=hint)

    authhelp = ui.config("commitcloud", "auth_help")
    if authhelp:
        ui.status(authhelp + "\n")
    # ui.prompt doesn't set up the prompt correctly, so pasting long lines
    # wraps incorrectly in the terminal.  Print the prompt on its own line
    # to avoid this.
    prompt = _("paste your commit cloud authentication token below:\n")
    ui.write(ui.label(prompt, "ui.prompt"))
    token = ui.prompt("", default="").strip()
    if token:
        service.get(ui, token).check()
        tokenlocator.settoken(token)
        ui.status(_("authentication successful\n"))


def checkauthenticated(ui, repo, tokenlocator):
    """check if authentication is needed"""
    token = tokenlocator.token
    if token:
        try:
            service.get(ui, token).check()
        except commitcloudcommon.RegistrationError:
            pass
        else:
            return
    authenticate(ui, repo, tokenlocator)


def getrepoworkspace(ui, repo):
    """get the workspace identity for the currently joined workspace"""
    workspacemanager = commitcloudutil.WorkspaceManager(repo)
    reponame = workspacemanager.reponame
    if not reponame:
        raise commitcloudcommon.ConfigurationError(ui, _("unknown repo"))

    workspace = workspacemanager.workspace
    if not workspace:
        raise commitcloudcommon.WorkspaceError(ui, _("undefined workspace"))

    return reponame, workspace


def getdefaultrepoworkspace(ui, repo):
    """get default workspace identity
       repo may be not joined to this workspace yet
    """
    workspacemanager = commitcloudutil.WorkspaceManager(repo)
    reponame = workspacemanager.reponame
    if not reponame:
        raise commitcloudcommon.ConfigurationError(ui, _("unknown repo"))
    return reponame, workspacemanager.defaultworkspace


@subcmd(
    "sync",
    [
        (
            "",
            "workspace-version",
            "",
            _(
                "target workspace version to sync to "
                "(skip `cloud sync` if the current version is greater or equal than the given one) (EXPERIMENTAL)"
            ),
        ),
        (
            "",
            "check-autosync-enabled",
            None,
            _(
                "check automatic synchronization settings "
                "(skip `cloud sync` if automatic synchronization is disabled) (EXPERIMENTAL)"
            ),
        ),
        (
            "",
            "use-bgssh",
            None,
            _(
                "try to use the password-less login for ssh if defined in the cconfig "
                "(option requires infinitepush.bgssh config) (EXPERIMENTAL)"
            ),
        ),
    ]
    + pullopts
    + pushopts,
)
def cloudsync(ui, repo, checkbackedup=None, cloudrefs=None, **opts):
    """synchronize commits with the commit cloud service
    """
    # external services can run cloud sync and require to check if
    # auto sync is enabled
    if opts.get("check_autosync_enabled") and not autosyncenabled(ui, repo):
        highlightstatus(
            ui, _("automatic backup and synchronization is currently disabled\n")
        )
        return 0

    repo.ignoreautobackup = True
    if opts.get("use_bgssh"):
        bgssh = ui.config("infinitepush", "bgssh")
        if bgssh:
            ui.setconfig("ui", "ssh", bgssh)

    lock = None
    # check if the background sync is running and provide all the details
    if ui.interactive():
        try:
            lock = lockmod.lock(repo.sharedvfs, _backuplockname, 0)
        except error.LockHeld as e:
            if e.errno == errno.ETIMEDOUT and e.lockinfo.isrunning():
                etimemsg = ""
                etime = commitcloudutil.getprocessetime(e.lockinfo)
                if etime:
                    etimemsg = _(", running for %d min %d sec") % divmod(etime, 60)
                highlightstatus(
                    ui,
                    _("background cloud sync is already in progress (pid %s on %s%s)\n")
                    % (e.lockinfo.uniqueid, e.lockinfo.namespace, etimemsg),
                )
                ui.flush()

    # run cloud sync with waiting for background process to complete
    try:
        # wait at most 120 seconds, because cloud sync can take a while
        timeout = 120
        with lock or lockmod.lock(
            repo.sharedvfs,
            _backuplockname,
            timeout=timeout,
            ui=ui,
            showspinner=True,
            spinnermsg=_("waiting for background process to complete"),
        ):
            currentnode = repo["."].node()
            _docloudsync(ui, repo, checkbackedup, cloudrefs, **opts)
            return _maybeupdateworkingcopy(ui, repo, currentnode)
    except error.LockHeld as e:
        if e.errno == errno.ETIMEDOUT:
            ui.warn(_("timeout waiting %d sec on backup lock expired\n") % timeout)
            return 2
        else:
            raise


def _docloudsync(ui, repo, checkbackedup=False, cloudrefs=None, **opts):
    start = time.time()

    tokenlocator = commitcloudutil.TokenLocator(ui)
    reponame, workspace = getrepoworkspace(ui, repo)
    serv = service.get(ui, tokenlocator.token)
    highlightstatus(ui, _("synchronizing '%s' with '%s'\n") % (reponame, workspace))
    commitcloudutil.writesyncprogress(
        repo, "starting synchronizing with '%s'" % workspace
    )

    def directfetching(heads):
        def unbundleall(bundlefiles):
            commands.unbundle(ui, repo, bundlefiles[0], *bundlefiles[1:], **opts)

        serv.getbundles(reponame, heads, unbundleall)

    # external services can know that fast pool is preferable to try
    pullfn = None
    if opts.get("direct_fetching"):
        if ui.configbool("commitcloud", "use_direct_bundle_fetching") and ui.config(
            "commitcloud", "get_command"
        ):
            pullfn = directfetching
        else:
            if not ui.config("commitcloud", "get_command"):
                ui.warn(
                    _(
                        "can't use direct fetching because 'commitcloud.get_command' is not set\n"
                    )
                )

    lastsyncstate = state.SyncState(repo, workspace)

    # external services can run cloud sync and know the lasest version
    version = opts.get("workspace_version")
    if version and version.isdigit() and int(version) <= lastsyncstate.version:
        highlightstatus(ui, _("this version has been already synchronized\n"))
        return 0

    if opts.get("full"):
        maxage = None
    else:
        maxage = ui.configint("commitcloud", "max_sync_age", None)
    fetchversion = lastsyncstate.version

    # cloudrefs are passed in cloud rejoin
    if cloudrefs is None:
        # if we are doing a full sync, or maxage has changed since the last
        # sync, use 0 as the last version to get a fresh copy of the full state.
        if maxage != lastsyncstate.maxage:
            fetchversion = 0
        cloudrefs = serv.getreferences(reponame, workspace, fetchversion)

    pushrevspec = calcpushrevfilter(ui, repo, workspace, opts)
    synced = False
    pushfailures = set()
    while not synced:
        if cloudrefs.version != fetchversion:
            _applycloudchanges(ui, repo, lastsyncstate, cloudrefs, pullfn, maxage)

        # Check if any omissions are now included in the repo
        _checkomissions(ui, repo, lastsyncstate)

        localheads = _getheads(repo)
        localbookmarks = _getbookmarks(repo)
        obsmarkers = commitcloudutil.getsyncingobsmarkers(repo)

        # Work out what we should have synced locally (and haven't deliberately
        # omitted)
        omittedheads = set(lastsyncstate.omittedheads)
        omittedbookmarks = set(lastsyncstate.omittedbookmarks)
        localsyncedheads = [
            head for head in lastsyncstate.heads if head not in omittedheads
        ]
        localsyncedbookmarks = {
            name: node
            for name, node in lastsyncstate.bookmarks.items()
            if name not in omittedbookmarks
        }

        if not obsmarkers:
            # If the heads have changed, and we don't have any obsmakers to
            # send, then it's possible we have some obsoleted versions of
            # commits that are visible in the cloud workspace that need to
            # be revived.
            cloudvisibleonly = repo.unfiltered().set(
                "draft() & ::%ls & hidden()", localsyncedheads
            )
            repo._commitcloudskippendingobsmarkers = True
            obsolete.revive(cloudvisibleonly)
            repo._commitcloudskippendingobsmarkers = False
            localheads = _getheads(repo)

        if pushrevspec:
            revs = scmutil.revrange(repo, pushrevspec)
            pushheads = [ctx.hex() for ctx in repo.set("heads(%ld::)", revs)]
            if not pushheads:
                highlightdebug(ui, _("revset doesn't match anything\n"))
            localheads = _filterpushside(
                ui, repo, pushheads, localheads, lastsyncstate.heads
            )

        if (
            set(localheads) == set(localsyncedheads)
            and localbookmarks == localsyncedbookmarks
            and lastsyncstate.version != 0
            and not obsmarkers
        ):
            synced = True

        if not synced:
            # The local repo has changed.  We must send these changes to the
            # cloud.
            path = commitcloudutil.getremotepath(repo, ui, None)

            def getconnection():
                return repo.connectionpool.get(path, opts)

            # Push commits that the server doesn't have.
            newheads = list(set(localheads) - set(lastsyncstate.heads))

            # if we are pushing too much it makes sense to check with the server first
            nocheckbackeduplimit = ui.configint("commitcloud", "nocheckbackeduplimit")

            # Fast server-side check of what hasn't been pushed yet
            if checkbackedup or len(newheads) > nocheckbackeduplimit:
                newheads = serv.filterpushedheads(reponame, newheads)

            # all pushed to the server except maybe obsmarkers
            allpushed = (not newheads) and (localbookmarks == localsyncedbookmarks)

            failedheads = []
            unfi = repo.unfiltered()
            if not allpushed:
                oldheads = list(
                    set(lastsyncstate.heads) - set(lastsyncstate.omittedheads)
                )
                backingup = [
                    nodemod.hex(n)
                    for n in unfi.nodes("draft() & ::%ls - ::%ls", newheads, oldheads)
                ]
                if len(backingup) == 1:
                    commitcloudutil.writesyncprogress(
                        repo, "backing up %s" % backingup[0][:12], backingup=backingup
                    )
                else:
                    commitcloudutil.writesyncprogress(
                        repo,
                        "backing up %d commits" % len(backingup),
                        backingup=backingup,
                    )
                newheads, failedheads = infinitepush.pushbackupbundlestacks(
                    ui, repo, getconnection, newheads
                )

            if failedheads:
                pushfailures |= set(failedheads)
                # Some heads failed to be pushed.  Work out what is actually
                # available on the server
                localheads = [
                    ctx.hex()
                    for ctx in unfi.set(
                        "heads((draft() & ::%ls) + (draft() & ::%ls & not obsolete()))",
                        newheads,
                        localsyncedheads,
                    )
                ]
                failedcommits = {
                    ctx.hex()
                    for ctx in repo.set(
                        "(draft() & ::%ls) - (draft() & ::%ls)", failedheads, localheads
                    )
                }
                # Revert any bookmark updates that refer to failed commits to
                # the available commits.
                for name, bookmarknode in localbookmarks.items():
                    if bookmarknode in failedcommits:
                        if name in lastsyncstate.bookmarks:
                            localbookmarks[name] = lastsyncstate.bookmarks[name]
                        else:
                            del localbookmarks[name]

            # Update the infinitepush backup bookmarks to point to the new
            # local heads and bookmarks.  This must be done after all
            # referenced commits have been pushed to the server.
            if not allpushed:
                pushbackupbookmarks(
                    ui, repo, getconnection, localheads, localbookmarks, **opts
                )

            # Work out the new cloud heads and bookmarks by merging in the
            # omitted items.  We need to preserve the ordering of the cloud
            # heads so that smartlogs generally match.
            newcloudheads = [
                head
                for head in lastsyncstate.heads
                if head in set(localheads) | set(lastsyncstate.omittedheads)
            ]
            newcloudheads.extend(
                [head for head in localheads if head not in set(newcloudheads)]
            )
            newcloudbookmarks = {
                name: localbookmarks.get(name, lastsyncstate.bookmarks.get(name))
                for name in set(localbookmarks.keys())
                | set(lastsyncstate.omittedbookmarks)
            }
            newomittedheads = list(set(newcloudheads) - set(localheads))
            newomittedbookmarks = list(
                set(newcloudbookmarks.keys()) - set(localbookmarks.keys())
            )

            # Update the cloud heads, bookmarks and obsmarkers.
            commitcloudutil.writesyncprogress(
                repo, "finishing synchronizing with '%s'" % workspace
            )
            synced, cloudrefs = serv.updatereferences(
                reponame,
                workspace,
                lastsyncstate.version,
                lastsyncstate.heads,
                newcloudheads,
                lastsyncstate.bookmarks.keys(),
                newcloudbookmarks,
                obsmarkers,
            )
            if synced:
                lastsyncstate.update(
                    cloudrefs.version,
                    newcloudheads,
                    newcloudbookmarks,
                    newomittedheads,
                    newomittedbookmarks,
                    maxage,
                )
                if obsmarkers:
                    commitcloudutil.clearsyncingobsmarkers(repo)

    commitcloudutil.writesyncprogress(repo)
    elapsed = time.time() - start
    highlightdebug(ui, _("cloudsync completed in %0.2f sec\n") % elapsed)
    if pushfailures:
        raise commitcloudcommon.SynchronizationError(
            ui, _("%d heads could not be pushed") % len(pushfailures)
        )
    highlightstatus(ui, _("commits synchronized\n"))
    # check that Scm Service is running and a subscription exists
    commitcloudutil.SubscriptionManager(repo).checksubscription()


def _maybeupdateworkingcopy(ui, repo, currentnode):
    if repo["."].node() != currentnode:
        return 0

    destination = finddestinationnode(repo, currentnode)

    if destination == currentnode:
        return 0

    if destination and destination in repo:
        highlightstatus(
            ui,
            _("current revision %s has been moved remotely to %s\n")
            % (nodemod.short(currentnode), nodemod.short(destination)),
        )
        if ui.configbool("commitcloud", "updateonmove"):
            if repo[destination].mutable():
                commitcloudutil.writesyncprogress(
                    repo,
                    "updating %s from %s to %s"
                    % (
                        repo.wvfs.base,
                        nodemod.short(currentnode),
                        nodemod.short(destination),
                    ),
                )
                return _update(ui, repo, destination)
        else:
            hintutil.trigger("commitcloud-update-on-move")
    else:
        highlightstatus(
            ui,
            _(
                "current revision %s has been replaced remotely "
                "with multiple revisions\n"
                "Please run `hg update` to go to the desired revision\n"
            )
            % nodemod.short(currentnode),
        )
    return 0


@subcmd("recover", [] + pullopts + pushopts)
def cloudrecover(ui, repo, **opts):
    """perform recovery for commit cloud

    Clear the local cache of commit cloud service state, and resynchronize
    the repository from scratch.
    """
    highlightstatus(ui, "clearing local commit cloud cache\n")
    _, workspace = getrepoworkspace(ui, repo)
    state.SyncState.erasestate(repo, workspace)
    cloudsync(ui, repo, checkbackedup=True, **opts)


def _applycloudchanges(
    ui, repo, lastsyncstate, cloudrefs, directfetchingfn=None, maxage=None
):
    pullcmd, pullopts = _getcommandandoptions("^pull")

    try:
        remotenames = extensions.find("remotenames")
    except KeyError:
        remotenames = None

    # Pull all the new heads and any bookmark hashes we don't have. We need to
    # filter cloudrefs before pull as pull does't check if a rev is present
    # locally.  Note that bookmarked public hashes can't use the
    # directfetchingfn fastpath.
    unfi = repo.unfiltered()
    newheads = [head for head in cloudrefs.heads if head not in unfi]
    if maxage is not None and maxage >= 0:
        mindate = time.time() - maxage * 86400
        omittedheads = [
            head
            for head in newheads
            if head in cloudrefs.headdates and cloudrefs.headdates[head] < mindate
        ]
        newheads = [head for head in newheads if head not in omittedheads]
    else:
        omittedheads = []
    omittedbookmarks = []

    if len(newheads) > 1:
        commitcloudutil.writesyncprogress(
            repo, "pulling %d new heads" % len(newheads), newheads=newheads
        )
    elif len(newheads) == 1:
        commitcloudutil.writesyncprogress(
            repo, "pulling %s" % nodemod.short(newheads[0]), newheads=newheads
        )

    if newheads and not directfetchingfn:
        # Replace the exchange pullbookmarks function with one which updates the
        # user's synced bookmarks.  This also means we don't partially update a
        # subset of the remote bookmarks if they happen to be included in the
        # pull.
        def _pullbookmarks(orig, pullop):
            if "bookmarks" in pullop.stepsdone:
                return
            pullop.stepsdone.add("bookmarks")
            tr = pullop.gettransaction()
            omittedbookmarks.extend(
                _mergebookmarks(pullop.repo, tr, cloudrefs.bookmarks, lastsyncstate)
            )

        # Replace the exchange pullobsolete function with one which adds the
        # cloud obsmarkers to the repo.
        def _pullobsolete(orig, pullop):
            if "obsmarkers" in pullop.stepsdone:
                return
            pullop.stepsdone.add("obsmarkers")
            tr = pullop.gettransaction()
            _mergeobsmarkers(pullop.repo, tr, cloudrefs.obsmarkers)

        # Disable pulling of remotenames.
        def _pullremotenames(orig, repo, remote, bookmarks):
            pass

        pullopts["rev"] = newheads
        with extensions.wrappedfunction(
            exchange, "_pullobsolete", _pullobsolete
        ), extensions.wrappedfunction(
            exchange, "_pullbookmarks", _pullbookmarks
        ), extensions.wrappedfunction(
            remotenames, "pullremotenames", _pullremotenames
        ) if remotenames else util.nullcontextmanager():
            pullcmd(ui, repo, **pullopts)
    else:
        if newheads and directfetchingfn:
            directfetchingfn(newheads)
        with repo.wlock(), repo.lock(), repo.transaction("cloudsync") as tr:
            omittedbookmarks.extend(
                _mergebookmarks(repo, tr, cloudrefs.bookmarks, lastsyncstate)
            )
            _mergeobsmarkers(repo, tr, cloudrefs.obsmarkers)

    # We have now synced the repo to the cloud version.  Store this.
    lastsyncstate.update(
        cloudrefs.version,
        cloudrefs.heads,
        cloudrefs.bookmarks,
        omittedheads,
        omittedbookmarks,
        maxage,
    )

    # Also update infinitepush state.  These new heads are already backed up,
    # otherwise the server wouldn't have told us about them.
    recordbackup(ui, repo, newheads)


def _checkomissions(ui, repo, lastsyncstate):
    """check omissions are still not available locally

    Check that the commits that have been deliberately omitted are still not
    available locally.  If they are now available (e.g. because the user pulled
    them manually), then remove the tracking of those heads being omitted, and
    restore any bookmarks that can now be restored.
    """
    lastomittedheads = set(lastsyncstate.omittedheads)
    lastomittedbookmarks = set(lastsyncstate.omittedbookmarks)
    omittedheads = set()
    omittedbookmarks = set()
    changes = []
    for head in lastomittedheads:
        if head not in repo:
            omittedheads.add(head)
    for name in lastomittedbookmarks:
        # bookmark might be removed from cloud workspace by someone else
        if name not in lastsyncstate.bookmarks:
            continue
        node = lastsyncstate.bookmarks[name]
        if node in repo:
            changes.append((name, nodemod.bin(node)))
        else:
            omittedbookmarks.add(name)
    if omittedheads != lastomittedheads or omittedbookmarks != lastomittedbookmarks:
        lastsyncstate.update(
            lastsyncstate.version,
            lastsyncstate.heads,
            lastsyncstate.bookmarks,
            list(omittedheads),
            list(omittedbookmarks),
            lastsyncstate.maxage,
        )
    if changes:
        with repo.wlock(), repo.lock(), repo.transaction("cloudsync") as tr:
            repo._bookmarks.applychanges(repo, tr, changes)


def _update(ui, repo, destination):
    # update to new head with merging local uncommited changes
    ui.status(_("updating to %s\n") % nodemod.short(destination))
    updatecheck = "noconflict"
    return hg.updatetotally(ui, repo, destination, destination, updatecheck=updatecheck)


def _filterpushside(ui, repo, pushheads, localheads, lastsyncstateheads):
    """filter push side to include only the specified push heads to the delta"""

    # local - allowed - synced
    skipped = set(localheads) - set(pushheads) - set(lastsyncstateheads)
    if skipped:

        def firstline(hexnode):
            return templatefilters.firstline(repo[hexnode].description())[:50]

        skippedlist = "\n".join(
            ["    %s    %s" % (hexnode[:16], firstline(hexnode)) for hexnode in skipped]
        )
        highlightstatus(
            ui,
            _("push filter: list of unsynced local heads that will be skipped\n%s\n")
            % skippedlist,
        )

    return list(set(localheads) & (set(lastsyncstateheads) | set(pushheads)))


def _mergebookmarks(repo, tr, cloudbookmarks, lastsyncstate):
    """merge any changes to the cloud bookmarks with any changes to local ones

    This performs a 3-way diff between the old cloud bookmark state, the new
    cloud bookmark state, and the local bookmark state.  If either local or
    cloud bookmarks have been modified, propagate those changes to the other.
    If both have been modified then fork the bookmark by renaming the local one
    and accepting the cloud bookmark's new value.

    Some of the bookmark changes may not be possible to apply, as the bookmarked
    commit has been omitted locally.  In that case the bookmark is omitted.

    Returns a list of the omitted bookmark names.
    """
    localbookmarks = _getbookmarks(repo)
    omittedbookmarks = set(lastsyncstate.omittedbookmarks)
    changes = []
    allnames = set(localbookmarks.keys() + cloudbookmarks.keys())
    newnames = set()
    for name in allnames:
        # We are doing a 3-way diff between the local bookmark and the cloud
        # bookmark, using the previous cloud bookmark's value as the common
        # ancestor.
        localnode = localbookmarks.get(name)
        cloudnode = cloudbookmarks.get(name)
        lastcloudnode = lastsyncstate.bookmarks.get(name)
        if cloudnode != localnode:
            # The local and cloud bookmarks differ, so we must merge them.

            # First, check if there is a conflict.
            if (
                localnode is not None
                and cloudnode is not None
                and localnode != lastcloudnode
                and cloudnode != lastcloudnode
            ):
                # The bookmark has changed both locally and remotely.  Fork the
                # bookmark by renaming the local one.
                forkname = _forkname(repo.ui, name, allnames | newnames)
                newnames.add(forkname)
                changes.append((forkname, nodemod.bin(localnode)))
                repo.ui.warn(
                    _(
                        "%s changed locally and remotely, "
                        "local bookmark renamed to %s\n"
                    )
                    % (name, forkname)
                )

            # If the cloud bookmarks has changed, we must apply its changes
            # locally.
            if cloudnode != lastcloudnode:
                if cloudnode is not None:
                    # The cloud bookmark has been set to point to a new commit.
                    if cloudnode in repo:
                        # The commit is available locally, so update the
                        # bookmark.
                        changes.append((name, nodemod.bin(cloudnode)))
                        omittedbookmarks.discard(name)
                    else:
                        # The commit is not available locally.  Omit it.
                        repo.ui.warn(
                            _("%s not found, omitting %s bookmark\n")
                            % (cloudnode, name)
                        )
                        omittedbookmarks.add(name)
                else:
                    # The bookmarks has been deleted in the cloud.
                    if localnode is not None and localnode != lastcloudnode:
                        # Although it has been deleted in the cloud, it has
                        # been moved in the repo at the same time.  Allow the
                        # local bookmark to persist - this will mean it is
                        # resurrected at the new local location.
                        pass
                    else:
                        # Remove the bookmark locally.
                        changes.append((name, None))

    repo._bookmarks.applychanges(repo, tr, changes)
    return list(omittedbookmarks)


def _mergeobsmarkers(repo, tr, obsmarkers):
    tr._commitcloudskippendingobsmarkers = True
    repo.obsstore.add(tr, obsmarkers)


def _forkname(ui, name, othernames):
    hostname = ui.config("commitcloud", "hostname", socket.gethostname())

    # Strip off any old suffix.
    m = re.match("-%s(-[0-9]*)?$" % re.escape(hostname), name)
    if m:
        suffix = "-%s%s" % (hostname, m.group(1) or "")
        name = name[0 : -len(suffix)]

    # Find a new name.
    for n in itertools.count():
        candidate = "%s-%s%s" % (name, hostname, "-%s" % n if n != 0 else "")
        if candidate not in othernames:
            return candidate


def _getheads(repo):
    headsrevset = repo.set("heads(draft()) & not obsolete()")
    return [ctx.hex() for ctx in headsrevset]


def _getbookmarks(repo):
    return {n: nodemod.hex(v) for n, v in repo._bookmarks.items()}


def _getcommandandoptions(command):
    cmd = commands.table[command][0]
    opts = dict(opt[1:3] for opt in commands.table[command][1])
    return cmd, opts


def getsuccessorsnodes(repo, node):
    successors = repo.obsstore.successors.get(node, ())
    for successor in successors:
        m = obsutil.marker(repo, successor)
        for snode in m.succnodes():
            if snode and snode != node:
                yield snode


def finddestinationnode(repo, node, visited=set()):
    visited.add(node)
    nodes = list(getsuccessorsnodes(repo, node))
    if len(nodes) == 1:
        node = nodes[0]
        if node in visited:
            repo.ui.status(
                _(
                    'obs-cycle detected (happens for "divergence" cases like A obsoletes B; B obsoletes A)\n'
                )
            )
            return None
        return finddestinationnode(repo, node)
    if len(nodes) == 0:
        return node
    return None


def pushbackupbookmarks(ui, repo, getconnection, localheads, localbookmarks, **opts):
    """
    Push a backup bundle to the server that updates the infinitepush backup
    bookmarks.

    This keeps the old infinitepush backup bookmarks in sync, which means
    pullbackup still works for users using commit cloud sync.
    """
    # Build a dictionary of infinitepush bookmarks.  We delete
    # all bookmarks and replace them with the full set each time.
    if infinitepushbackup:
        infinitepushbookmarks = {}
        namingmgr = infinitepushbackup.BackupBookmarkNamingManager(
            ui, repo, opts.get("user")
        )
        infinitepushbookmarks[namingmgr.getbackupheadprefix()] = ""
        infinitepushbookmarks[namingmgr.getbackupbookmarkprefix()] = ""
        for bookmark, hexnode in localbookmarks.items():
            name = namingmgr.getbackupbookmarkname(bookmark)
            infinitepushbookmarks[name] = hexnode
        for hexhead in localheads:
            name = namingmgr.getbackupheadname(hexhead)
            infinitepushbookmarks[name] = hexhead

        # Push a bundle containing the new bookmarks to the server.
        with getconnection() as conn:
            infinitepush.pushbackupbundle(
                ui, repo, conn.peer, None, infinitepushbookmarks
            )

        # Update the infinitepush local state.
        infinitepushbackup._writelocalbackupstate(
            repo.sharedvfs, list(localheads), localbookmarks
        )


def recordbackup(ui, repo, newheads):
    """Record that the given heads are already backed up."""
    if infinitepushbackup is None:
        return

    backupstate = infinitepushbackup._readlocalbackupstate(ui, repo)
    backupheads = set(backupstate.heads) | set(newheads)
    infinitepushbackup._writelocalbackupstate(
        repo.sharedvfs, list(backupheads), backupstate.localbookmarks
    )


def autosyncenabled(ui, _repo):
    return infinitepushbackup is not None and infinitepushbackup.autobackupenabled(ui)


def backuplockcheck(ui, repo):
    try:
        with lockmod.trylock(ui, repo.sharedvfs, _backuplockname, 0, 0):
            pass
    except error.LockHeld as e:
        if e.lockinfo.isrunning():
            lockinfo = e.lockinfo
            etime = commitcloudutil.getprocessetime(lockinfo)
            if etime:
                minutes, seconds = divmod(etime, 60)
                etimemsg = _("\n(pid %s on %s, running for %d min %d sec)") % (
                    lockinfo.uniqueid,
                    lockinfo.namespace,
                    minutes,
                    seconds,
                )
            else:
                etimemsg = ""
            bgstep = commitcloudutil.getsyncprogress(repo) or "synchronizing"
            highlightstatus(
                ui,
                _("background cloud sync is in progress: %s%s\n") % (bgstep, etimemsg),
            )


def calcpushrevfilter(ui, repo, workspace, opts):
    """build a filter to figure out what unsynced commits to send to the server

    This allows `cloud sync` to skip some local commits on any machine if configured
    """
    revspec = None
    # command option has precedence
    # multiple is allowed (will be union)
    if opts.get("push_revs"):
        revspec = opts.get("push_revs")
    # configuration options (effective for the default workspace only)
    # (will be intersection)
    elif workspace == getdefaultrepoworkspace(ui, repo)[1]:
        collect = []
        if ui.configbool("commitcloud", "user_commits_only"):
            collect.append("author(%s)" % util.emailuser(ui.username()))
        if ui.config("commitcloud", "custom_push_revs"):
            collect.append("(%s)" % ui.config("commitcloud", "custom_push_revs"))
        if collect:
            revspec = ["&".join(["draft()"] + collect)]
    if not revspec:
        return None
    # check if rev spec makes any sense
    # clean up the filter if it doesn't filter anything out
    # this is useful until better performance of heads(%ld::)
    if len(revspec) == 1 and not next(repo.set("draft()-(%s)" % revspec[0]), None):
        return None
    return revspec


def missingcloudrevspull(repo, nodes):
    """pull wrapper for changesets that are known to the obstore and unknown for the repo

    This is, for example, the case for all hidden revs on new clone + cloud sync.
    """
    unfi = repo.unfiltered()

    def obscontains(nodebin):
        return bool(unfi.obsstore.successors.get(nodebin, None))

    nodes = [node for node in nodes if node not in unfi and obscontains(node)]
    if nodes:
        pullcmd, pullopts = _getcommandandoptions("^pull")
        pullopts["rev"] = [nodemod.hex(node) for node in nodes]
        pullcmd(repo.ui, unfi, **pullopts)

    return nodes
