import os
from binascii import hexlify

from pony.orm import db_session
from twisted.internet import task, reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall

from Tribler.Core.DownloadConfig import DownloadStartupConfig
from Tribler.Core.Modules.MetadataStore.OrmBindings.channel_node import COMMITTED
from Tribler.Core.TorrentDef import TorrentDefNoMetainfo, TorrentDef
from Tribler.Core.simpledefs import DLSTATUS_SEEDING
from Tribler.pyipv8.ipv8.taskmanager import TaskManager


class GigaChannelManager(TaskManager):
    """
    This class represents the main manager for gigachannels.
    It provides methods to manage channels, download new channels or remove existing ones.
    """

    def __init__(self, session):
        super(GigaChannelManager, self).__init__()
        self.session = session

    def start(self):
        """
        The Metadata Store checks the database at regular intervals to see if new channels are available for preview
        or subscribed channels require updating.
        """
        self.updated_my_channel()  # Just in case

        channels_check_interval = 5.0  # seconds
        lcall = LoopingCall(self.service_channels)
        d = self.register_task("Process channels download queue and remove cruft", lcall).start(channels_check_interval)

        # def handle(f):
        #    print "errback"
        #    print "we got an exception: %s" % (f.getTraceback(),)
        #    f.trap(RuntimeError)
        # d.addErrback(handle)

    def shutdown(self):
        """
        Stop the gigachannel manager.
        """
        self.shutdown_task_manager()

    def remove_cruft_channels(self):
        """
        Assembles a list of obsolete channel torrents to be removed.
        The list is formed from older versions of channels we are subscribed to and from channel torrents we are not
        subscribed to (i.e. we recently unsubscribed from these). The unsubscribed channels are removed completely
        with their contents, while in the case of older versions the files are left in place because the newer version
        possibly uses them.
        :return: list of tuples (download_to_remove=download, remove_files=Bool)
        """
        with db_session:
            channels, _ = self.session.lm.mds.ChannelMetadata.get_channels(last=10000, subscribed=True)
            subscribed_infohashes = [bytes(c.infohash) for c in list(channels)]
            dirnames = [c.dir_name for c in channels]

        # TODO: add some more advanced logic for removal of older channel versions
        cruft_list = [(d, d.get_def().get_name_utf8() not in dirnames) \
                      for d in self.session.lm.get_channel_downloads() \
                      if bytes(d.get_def().infohash) not in subscribed_infohashes]
        self.remove_channels_downloads(cruft_list)

    def service_channels(self):
        try:
            self.remove_cruft_channels()
        except:
            pass
        try:
            self.check_channels_updates()
        except:
            pass

    def check_channels_updates(self):
        """
        Check whether there are channels that are updated. If so, download the new version of the channel.
        """
        # FIXME: These naughty try-except-pass workarounds are necessary to keep the loop going in all circumstances

        with db_session:
            channels_queue = list(self.session.lm.mds.ChannelMetadata.get_updated_channels())

        for channel in channels_queue:
            try:
                if not self.session.has_download(str(channel.infohash)):
                    self._logger.info("Downloading new channel version %s ver %i->%i",
                                      str(channel.public_key).encode("hex"),
                                      channel.local_version, channel.timestamp)
                    self.download_channel(channel)
            except:
                pass

    def on_channel_download_finished(self, download, channel_id, finished_deferred=None):
        """
        We have finished with downloading a channel.
        :param download: The channel download itself.
        :param channel_id: The ID of the channel.
        :param finished_deferred: An optional deferred that should fire if the channel download has finished.
        """
        if download.finished_callback_already_called:
            return
        channel_dirname = os.path.join(self.session.lm.mds.channels_dir, download.get_def().get_name())
        self.session.lm.mds.process_channel_dir(channel_dirname, channel_id)
        if finished_deferred:
            finished_deferred.callback(download)

    @db_session
    def remove_channel(self, channel):
        """
        Remove a channel from your local database/download list.
        :param channel: The channel to remove.
        """
        channel.subscribed = False
        channel.remove_contents()
        channel.local_version = 0

        # Remove all stuff matching the channel dir name / public key / torrent title
        remove_list = [(d, True) for d in self.session.lm.get_channel_downloads() if
                       d.tdef.get_name_utf8() == channel.dir_name]
        self.remove_channels_downloads(remove_list)

    # TODO: finish this routine
    # This thing should check if the files in the torrent we're going to delete are used in another torrent for
    # a newer version of the same channel, and determine a safe sub-set to delete.
    """
    def safe_files_to_remove(self, download):
        # Check for intersection of files from old download with files from the newer version of the same channel
        dirname = download.get_def().get_name_utf8()
        files_to_remove = []
        with db_session:
            channel = self.session.lm.mds.ChannelMetadata.get_channel_with_dirname(dirname)
        if channel and channel.subscribed:
            print self.session.lm.downloads
            current_version = self.session.get_download(hexlify(channel.infohash))
            current_version_files = set(current_version.get_tdef().get_files())
            obsolete_version_files = set(download.get_tdef().get_files())
            files_to_remove_relative = obsolete_version_files - current_version_files
            for f in files_to_remove_relative:
                files_to_remove.append(os.path.join(dirname, f))
        return files_to_remove
    """

    def remove_channels_downloads(self, to_remove_list):
        """
        :param to_remove_list: list of tuples (download_to_remove=download, remove_files=Bool)
        """

        """
        files_to_remove = []
        for download in to_remove_list:
            files_to_remove.extend(self.safe_files_to_remove(download))
        """

        def _on_remove_failure(failure):
            self._logger.error("Error when removing the channel download: %s", failure)

        # removed_list = []
        for i, dl_tuple in enumerate(to_remove_list):
            d, remove_content = dl_tuple
            deferred = self.session.remove_download(d, remove_content=remove_content)
            deferred.addErrback(_on_remove_failure)
            self.register_task(u'remove_channel' + d.tdef.get_name_utf8() + u'-' + hexlify(d.tdef.get_infohash()) +
                               u'-' + str(i), deferred)
            # removed_list.append(deferred)

        """
        def _on_torrents_removed(torrent):
            print files_to_remove
        dl = DeferredList(removed_list)
        dl.addCallback(_on_torrents_removed)
        self.register_task(u'remove_channels_files-' + "_".join([d.tdef.get_name_utf8() for d in to_remove_list]), dl)
        """

    def download_channel(self, channel):
        """
        Download a channel with a given infohash and title.
        :param channel: The channel metadata ORM object.
        """
        finished_deferred = Deferred()

        dcfg = DownloadStartupConfig()
        dcfg.set_dest_dir(self.session.lm.mds.channels_dir)
        dcfg.set_channel_download(True)
        tdef = TorrentDefNoMetainfo(infohash=str(channel.infohash), name=channel.dir_name)
        download = self.session.start_download_from_tdef(tdef, dcfg)
        channel_id = channel.public_key
        # TODO: add errbacks here!
        download.finished_callback = lambda dl: self.on_channel_download_finished(dl, channel_id, finished_deferred)
        if download.get_state().get_status() == DLSTATUS_SEEDING and not download.finished_callback_already_called:
            download.finished_callback_already_called = True
            download.finished_callback(download)
        return download, finished_deferred

    def updated_my_channel(self):
        """
        Notify the core that we updated our channel.
        """
        try:
            my_channel = self.session.lm.mds.ChannelMetadata.get_my_channel()
            if my_channel and my_channel.status == COMMITTED and \
                    not self.session.has_download(str(my_channel.infohash)):
                torrent_path = os.path.join(self.session.lm.mds.channels_dir, my_channel.dir_name + ".torrent")
            else:
                return

            tdef = TorrentDef.load(torrent_path)
            dcfg = DownloadStartupConfig()
            dcfg.set_dest_dir(self.session.lm.mds.channels_dir)
            dcfg.set_channel_download(True)
            self.session.lm.add(tdef, dcfg)
        except:
            # Ugly recursive workaround for race condition when the torrent file is not there
            # FIXME: stop using intermediary torrent file for personal channel
            taskname = "updated_my_channel delayed attempt"
            if not self.is_pending_task_active(taskname):
                d = task.deferLater(reactor, 7, self.updated_my_channel)
                self.register_task(taskname, d)