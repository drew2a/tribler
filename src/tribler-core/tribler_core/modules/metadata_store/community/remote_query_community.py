import json
import time
from binascii import unhexlify
from random import getrandbits, sample

from ipv8.community import Community
from ipv8.lazy_community import lazy_wrapper
from ipv8.messaging.lazy_payload import VariablePayload, vp_compile
from ipv8.peer import Peer

from tribler_core.modules.metadata_store.orm_bindings.channel_metadata import entries_to_chunk
from tribler_core.modules.metadata_store.serialization import CHANNEL_TORRENT

# The maximum number of packets to receive from any given peer from a single request.
# This limit is imposed as a safety precaution to prevent spam/flooding
PACKETS_LIMIT = 10
REQUEST_TIMEOUT = 10  # seconds


@vp_compile
class RemoteSelectPayload(VariablePayload):
    msg_id = 1
    format_list = ['I', 'varlenH']
    names = ['id', 'json']


@vp_compile
class SelectResponsePayload(VariablePayload):
    msg_id = 2
    format_list = ['I', 'raw']
    names = ['id', 'raw_blob']


class RemoteQueryCommunitySettings:
    def __init__(self):
        self.minimal_blob_size = 200
        self.maximum_payload_size = 1300
        self.max_entries = self.maximum_payload_size // self.minimal_blob_size
        self.max_query_peers = 5


class RemoteQueryCommunity(Community):
    """
    Community for general purpose SELECT-like queries into remote Channels database
    """

    master_peer = Peer(
        unhexlify(
            "4c69624e61434c504b3a667b8dee4645475512c0780990cfaca234ad19c5dabcb065751776"
            "b75a4b4210c06e2eb4d8bbf4a775ed735eb16bbc3e44193479ad7426d7cd1067807f95b696"
        )
    )

    def __init__(self, my_peer, endpoint, network, metadata_store, settings=None):
        super(RemoteQueryCommunity, self).__init__(my_peer, endpoint, network)

        self.settings = settings or RemoteQueryCommunitySettings()

        self.mds = metadata_store
        # This dict stores information about outstanding requests in the form:
        # (peer.mid, request_id) : [request_args_dict, response_packets_limit, request_timeout_moment]
        # It is periodically cleaned up by self.evict_timed_out_requests method
        self.outstanding_requests = dict()

        self.add_message_handler(RemoteSelectPayload, self.on_remote_select)
        self.add_message_handler(SelectResponsePayload, self.on_remote_select_response)
        self.register_task("Timeout remote select requests", self.evict_timed_out_requests, interval=1)

    def get_random_peers(self, sample_size=None):
        # Randomly sample sample_size peers from the complete list of our peers
        all_peers = self.get_peers()
        if sample_size is not None and sample_size < len(all_peers):
            return sample(all_peers, sample_size)
        return all_peers

    def evict_timed_out_requests(self):
        now = time.time()
        for request_id, [_, _, request_timeout_moment] in list(self.outstanding_requests.items()):
            if now > request_timeout_moment:
                self.outstanding_requests.pop(request_id, None)

    def send_remote_select(self, peer, **kwargs):
        request_id = getrandbits(32)
        self.outstanding_requests[(peer.mid, request_id)] = [kwargs, PACKETS_LIMIT, time.time() + REQUEST_TIMEOUT]
        self.ez_send(peer, RemoteSelectPayload(request_id, json.dumps(kwargs).encode('utf8')))

    def send_remote_select_to_many(self, **kwargs):
        for p in self.get_random_peers(self.settings.max_query_peers):
            self.send_remote_select(p, **kwargs)

    def send_remote_select_subscribed_channels(self, peer):
        request_dict = {"metadata_type": [CHANNEL_TORRENT], "subscribed": True}
        self.send_remote_select(peer, **request_dict)

    @lazy_wrapper(RemoteSelectPayload)
    async def on_remote_select(self, peer, request):
        db_results = await self.mds.MetadataNode.get_entries_threaded(**json.loads(request.json))
        if not db_results:
            return

        index = 0
        while index < len(db_results):
            data, index = entries_to_chunk(db_results, self.settings.maximum_payload_size, start_index=index)
            self.ez_send(peer, SelectResponsePayload(request.id, data))

    @lazy_wrapper(SelectResponsePayload)
    async def on_remote_select_response(self, peer, response):
        request_id = (peer.mid, response.id)
        request = self.outstanding_requests.get(request_id, None)
        if not request:
            return

        now = time.time()
        request_dict, packet_limit, request_timeout_moment = request
        # Check that request did not reach a timeout
        if now > request_timeout_moment:
            self.outstanding_requests.pop(request_id, None)
            return

        # Check for limit on the number of packets per request
        if packet_limit > 1:
            self.outstanding_requests[request_id][1] = packet_limit - 1
        else:
            self.outstanding_requests.pop(request_id, None)

        # We use responses for requests about subscribed channels to bump our local channels ratings
        peer_vote = peer if request_dict.get("subscribed", None) is True else None

        await self.mds.process_compressed_mdblob_threaded(response.raw_blob, peer_vote_for_channels=peer_vote)


class RemoteQueryTestnetCommunity(RemoteQueryCommunity):
    master_peer = Peer(
        unhexlify(
            "4c69624e61434c504b3a7fcf64783215dba08c1623fb14c3c86127b8591f858c56763e2281"
            "a8e121ef08caae395b2597879f7f4658b608f22df280073661f85174fd7c565cbee3e4328f"
        )
    )
