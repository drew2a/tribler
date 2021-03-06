import random
from asyncio import get_event_loop
from binascii import unhexlify

from ipv8.community import Community
from ipv8.lazy_community import lazy_wrapper
from ipv8.peer import Peer

from pony.orm import db_session

from tribler_core.modules.popularity.payload import TorrentsHealthPayload
from tribler_core.utilities.unicode import hexlify

PUBLISH_INTERVAL = 5


class PopularityCommunity(Community):
    """
    Community for disseminating the content across the network. Follows publish-subscribe model.
    """
    community_id = unhexlify('9aca62f878969c437da9844cba29a134917e1648')

    def __init__(self, *args, **kwargs):
        self.metadata_store = kwargs.pop('metadata_store')
        self.torrent_checker = kwargs.pop('torrent_checker', None)

        super(PopularityCommunity, self).__init__(*args, **kwargs)

        self.add_message_handler(TorrentsHealthPayload, self.on_torrents_health)

        self.logger.info('Popularity Community initialized (peer mid %s)', hexlify(self.my_peer.mid))
        self.register_task("publish", self.gossip_torrents_health, interval=PUBLISH_INTERVAL)

    @db_session
    def gossip_torrents_health(self):
        """
        Gossip torrent health information to another peer.
        """
        if not self.get_peers() or not self.torrent_checker:
            return

        num_torrents_checked = len(self.torrent_checker.torrents_checked)
        random_torrents_checked = random.sample(self.torrent_checker.torrents_checked, min(num_torrents_checked, 5))
        popular_torrents_checked = sorted(self.torrent_checker.torrents_checked - set(random_torrents_checked),
                                          key=lambda tup: tup[1], reverse=True)[:5]

        random_peer = random.choice(self.get_peers())

        self.ez_send(random_peer, TorrentsHealthPayload.create(random_torrents_checked, popular_torrents_checked))

    @lazy_wrapper(TorrentsHealthPayload)
    async def on_torrents_health(self, _, payload):
        self.logger.info("Received torrent health information for %d random torrents and %d checked torrents",
                         len(payload.random_torrents), len(payload.torrents_checked))
        all_torrents = payload.random_torrents + payload.torrents_checked

        def _put_health_entries_in_db():
            with db_session:
                for infohash, seeders, leechers, last_check in all_torrents:
                    torrent_state = self.metadata_store.TorrentState.get(infohash=infohash)
                    if torrent_state and last_check > torrent_state.last_check:
                        # Replace current information
                        torrent_state.seeders = seeders
                        torrent_state.leechers = leechers
                        torrent_state.last_check = last_check
                    elif not torrent_state:
                        _ = self.metadata_store.TorrentState(infohash=infohash, seeders=seeders,
                                                             leechers=leechers, last_check=last_check)

            self.metadata_store.disconnect_thread()
        await get_event_loop().run_in_executor(None, _put_health_entries_in_db)
