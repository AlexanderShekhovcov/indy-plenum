import itertools
import os
import random
import string
from _signal import SIGINT
from functools import partial
from itertools import permutations, combinations
from shutil import copyfile
from sys import executable
from time import sleep
from typing import Tuple, Iterable, Dict, Optional, NamedTuple, \
    List, Any, Sequence
from typing import Union

from psutil import Popen

from ledger.genesis_txn.genesis_txn_file_util import genesis_txn_file
from plenum.client.client import Client
from plenum.client.wallet import Wallet
from plenum.common.constants import DOMAIN_LEDGER_ID, OP_FIELD_NAME, REPLY, REQACK, REQNACK, REJECT
from plenum.common.messages.node_messages import Reply, PrePrepare, Prepare, Commit
from plenum.common.request import Request
from plenum.common.types import f
from plenum.common.util import getMaxFailures, \
    checkIfMoreThanFSameItems, getNoInstances, get_utc_epoch
from plenum.server.node import Node
from plenum.test import waits
from plenum.test.msgs import randomMsg
from plenum.test.spy_helpers import getLastClientReqReceivedForNode, getAllArgs, \
    getAllReturnVals
from plenum.test.test_client import TestClient, genTestClient
from plenum.test.test_node import TestNode, TestReplica, TestNodeSet, \
    checkNodesConnected, ensureElectionsDone, NodeRef, getPrimaryReplica
from stp_core.common.log import getlogger
from stp_core.loop.eventually import eventuallyAll, eventually
from stp_core.loop.looper import Looper
from stp_core.network.util import checkPortAvailable

DelayRef = NamedTuple("DelayRef", [
    ("op", Optional[str]),
    ("frm", Optional[str])])

logger = getlogger()


# noinspection PyUnresolvedReferences


def ordinal(n):
    return "%d%s" % (
        n, "tsnrhtdd"[(n / 10 % 10 != 1) * (n % 10 < 4) * n % 10::4])


def checkSufficientRepliesReceived(receivedMsgs: Iterable,
                                   reqId: int,
                                   fValue: int):
    """
    Checks number of replies for request with specified id in given inbox and
    if this number is lower than number of malicious nodes (fValue) -
    raises exception

    If you do not need response ponder on using
    waitForSufficientRepliesForRequests instead

    :returns: response for request
    """

    receivedReplies = getRepliesFromClientInbox(inbox=receivedMsgs,
                                                reqId=reqId)
    logger.debug("received replies for reqId {}: {}".
                 format(reqId, receivedReplies))
    assert len(receivedReplies) > fValue, "Received {} replies but expected " \
                                          "at-least {} for reqId {}". \
        format(len(receivedReplies), fValue + 1, reqId)
    result = checkIfMoreThanFSameItems([reply[f.RESULT.nm] for reply in
                                        receivedReplies], fValue)
    assert result, "reqId {}: found less than in {} same replies".format(
        reqId, fValue)

    assert all([r[f.RESULT.nm][f.REQ_ID.nm] == reqId for r in receivedReplies]
               ), "not all replies have got reqId {}".format(reqId)

    return result
    # TODO add test case for what happens when replies don't have the same data


def waitForSufficientRepliesForRequests(looper,
                                        client,
                                        *,  # To force usage of names
                                        requests=None,
                                        requestIds=None,
                                        fVal=None,
                                        customTimeoutPerReq=None,
                                        add_delay_to_timeout: float = 0,
                                        override_timeout_limit=False,
                                        total_timeout=None):
    """
    Checks number of replies for given requests of specific client and
    raises exception if quorum not reached at least for one

    :requests: list of requests; mutually exclusive with 'requestIds'
    :requestIds:  list of request ids; mutually exclusive with 'requests'
    :returns: nothing
    """

    if requests is not None and requestIds is not None:
        raise ValueError("Args 'requests' and 'requestIds' are "
                         "mutually exclusive")
    requestIds = requestIds or [request.reqId for request in requests]

    nodeCount = len(client.nodeReg)
    fVal = fVal or getMaxFailures(nodeCount)

    if not total_timeout:
        timeoutPerRequest = customTimeoutPerReq or \
            waits.expectedTransactionExecutionTime(nodeCount)
        timeoutPerRequest += add_delay_to_timeout
        # here we try to take into account what timeout for execution
        # N request - total_timeout should be in
        # timeoutPerRequest < total_timeout < timeoutPerRequest * N
        # we cannot just take (timeoutPerRequest * N) because it is so huge.
        # (for timeoutPerRequest=5 and N=10, total_timeout=50sec)
        # lets start with some simple formula:
        total_timeout = (1 + len(requestIds) / 10) * timeoutPerRequest

    coros = []
    for requestId in requestIds:
        coros.append(partial(checkSufficientRepliesReceived,
                             client.inBox,
                             requestId,
                             fVal))

    chk_all_funcs(looper, coros, retry_wait=1, timeout=total_timeout,
                  override_eventually_timeout=override_timeout_limit)

    # looper.run(eventuallyAll(*coros,
    #                          retryWait=1,
    #                          totalTimeout=total_timeout,
    #                          override_timeout_limit=override_timeout_limit))


def sendReqsToNodesAndVerifySuffReplies(looper: Looper,
                                        wallet: Wallet,
                                        client: TestClient,
                                        numReqs: int,
                                        fVal: int=None,
                                        customTimeoutPerReq: float=None,
                                        add_delay_to_timeout: float=0,
                                        override_timeout_limit=False,
                                        total_timeout=None):
    nodeCount = len(client.nodeReg)
    fVal = fVal or getMaxFailures(nodeCount)
    requests = sendRandomRequests(wallet, client, numReqs)
    waitForSufficientRepliesForRequests(
        looper,
        client,
        requests=requests,
        fVal=fVal,
        customTimeoutPerReq=customTimeoutPerReq,
        add_delay_to_timeout=add_delay_to_timeout,
        override_timeout_limit=override_timeout_limit,
        total_timeout=total_timeout)
    return requests


def send_reqs_to_nodes_and_verify_all_replies(looper: Looper,
                                              wallet: Wallet,
                                              client: TestClient,
                                              numReqs: int,
                                              customTimeoutPerReq: float=None,
                                              add_delay_to_timeout: float=0,
                                              override_timeout_limit=False,
                                              total_timeout=None):
    requests = sendRandomRequests(wallet, client, numReqs)
    nodeCount = len(client.nodeReg)
    # wait till more than nodeCount replies are received (that is all nodes
    # answered)
    waitForSufficientRepliesForRequests(
        looper,
        client,
        requests=requests,
        fVal=nodeCount - 1,
        customTimeoutPerReq=customTimeoutPerReq,
        add_delay_to_timeout=add_delay_to_timeout,
        override_timeout_limit=override_timeout_limit,
        total_timeout=total_timeout)
    return requests


def send_reqs_batches_and_get_suff_replies(
        looper: Looper,
        wallet: Wallet,
        client: TestClient,
        num_reqs: int,
        num_batches=1,
        **kwargs):
    # This method assumes that `num_reqs` <= num_batches*MaxbatchSize
    if num_batches == 1:
        return sendReqsToNodesAndVerifySuffReplies(looper, wallet, client,
                                                   num_reqs, **kwargs)
    else:
        requests = []
        for _ in range(num_batches - 1):
            requests.extend(
                sendReqsToNodesAndVerifySuffReplies(
                    looper,
                    wallet,
                    client,
                    num_reqs //
                    num_batches,
                    **kwargs))
        rem = num_reqs % num_batches
        if rem == 0:
            rem = num_reqs // num_batches
        requests.extend(sendReqsToNodesAndVerifySuffReplies(looper, wallet,
                                                            client,
                                                            rem,
                                                            **kwargs))
        return requests


# noinspection PyIncorrectDocstring
def checkResponseCorrectnessFromNodes(receivedMsgs: Iterable, reqId: int,
                                      fValue: int) -> bool:
    """
    the client must get at least :math:`f+1` responses
    """
    msgs = [(msg[f.RESULT.nm][f.REQ_ID.nm], msg[f.RESULT.nm][f.IDENTIFIER.nm])
            for msg in getRepliesFromClientInbox(receivedMsgs, reqId)]
    groupedMsgs = {}
    for tpl in msgs:
        groupedMsgs[tpl] = groupedMsgs.get(tpl, 0) + 1
    assert max(groupedMsgs.values()) >= fValue + 1


def getRepliesFromClientInbox(inbox, reqId) -> list:
    return list({_: msg for msg, _ in inbox if
                 msg[OP_FIELD_NAME] == REPLY and msg[f.RESULT.nm]
                 [f.REQ_ID.nm] == reqId}.values())


def checkLastClientReqForNode(node: TestNode, expectedRequest: Request):
    recvRequest = getLastClientReqReceivedForNode(node)
    assert recvRequest
    assert expectedRequest.as_dict == recvRequest.as_dict


# noinspection PyIncorrectDocstring


def getPendingRequestsForReplica(replica: TestReplica, requestType: Any):
    return [item[0] for item in replica.postElectionMsgs if
            isinstance(item[0], requestType)]


def assertLength(collection: Iterable[Any], expectedLength: int):
    assert len(
        collection) == expectedLength, "Observed length was {} but " \
                                       "expected length was {}". \
        format(len(collection), expectedLength)


def assertEquality(observed: Any, expected: Any):
    assert observed == expected, "Observed value was {} but expected value " \
                                 "was {}".format(observed, expected)


def setupNodesAndClient(
        looper: Looper,
        nodes: Sequence[TestNode],
        nodeReg=None,
        tmpdir=None):
    looper.run(checkNodesConnected(nodes))
    ensureElectionsDone(looper=looper, nodes=nodes)
    return setupClient(looper, nodes, nodeReg=nodeReg, tmpdir=tmpdir)


def setupClient(looper: Looper,
                nodes: Sequence[TestNode] = None,
                nodeReg=None,
                tmpdir=None,
                identifier=None,
                verkey=None):
    client1, wallet = genTestClient(nodes=nodes,
                                    nodeReg=nodeReg,
                                    tmpdir=tmpdir,
                                    identifier=identifier,
                                    verkey=verkey)
    looper.add(client1)
    looper.run(client1.ensureConnectedToNodes())
    return client1, wallet


def setupClients(count: int,
                 looper: Looper,
                 nodes: Sequence[TestNode] = None,
                 nodeReg=None,
                 tmpdir=None):
    wallets = {}
    clients = {}
    for i in range(count):
        name = "test-wallet-{}".format(i)
        wallet = Wallet(name)
        idr, _ = wallet.addIdentifier()
        verkey = wallet.getVerkey(idr)
        client, _ = setupClient(looper,
                                nodes,
                                nodeReg,
                                tmpdir,
                                identifier=idr,
                                verkey=verkey)
        clients[client.name] = client
        wallets[client.name] = wallet
    return clients, wallets


# noinspection PyIncorrectDocstring
async def aSetupClient(looper: Looper,
                       nodes: Sequence[TestNode] = None,
                       nodeReg=None,
                       tmpdir=None):
    """
    async version of above
    """
    client1 = genTestClient(nodes=nodes,
                            nodeReg=nodeReg,
                            tmpdir=tmpdir)
    looper.add(client1)
    await client1.ensureConnectedToNodes()
    return client1


def randomOperation():
    return {
        "type": "buy",
        "amount": random.randint(10, 100)
    }


def random_requests(count):
    return [{
        "type": "buy",
                "amount": random.randint(10, 100)
    } for _ in range(count)]


def signed_random_requests(wallet, count):
    reqs = random_requests(count)
    return [wallet.signOp(req) for req in reqs]


def send_signed_requests(client: Client, signed_reqs: Sequence):
    return client.submitReqs(*signed_reqs)[0]


def sendRandomRequest(wallet: Wallet, client: Client):
    return sendRandomRequests(wallet, client, 1)[0]


def sendRandomRequests(wallet: Wallet, client: Client, count: int):
    logger.debug('Sending {} random requests'.format(count))
    return send_signed_requests(client,
                                signed_random_requests(wallet, count))


def buildCompletedTxnFromReply(request, reply: Reply) -> Dict:
    txn = request.operation
    txn.update(reply)
    return txn


async def msgAll(nodes: TestNodeSet):
    # test sending messages from every node to every other node
    # TODO split send and check so that the messages can be sent concurrently
    for p in permutations(nodes.nodeNames, 2):
        await sendMessageAndCheckDelivery(nodes, p[0], p[1])


def sendMessage(nodes: TestNodeSet,
                frm: NodeRef,
                to: NodeRef,
                msg: Optional[Tuple]=None):
    """
    Sends message from one node to another

    :param nodes:
    :param frm: sender
    :param to: recepient
    :param msg: optional message - by default random one generated
    :return:
    """

    logger.debug("Sending msg from {} to {}".format(frm, to))
    msg = msg if msg else randomMsg()
    sender = nodes.getNode(frm)
    rid = sender.nodestack.getRemote(nodes.getNodeName(to)).uid
    sender.nodestack.send(msg, rid)


async def sendMessageAndCheckDelivery(nodes: TestNodeSet,
                                      frm: NodeRef,
                                      to: NodeRef,
                                      msg: Optional[Tuple] = None,
                                      method=None,
                                      customTimeout=None):
    """
    Sends message from one node to another and checks that it was delivered

    :param nodes:
    :param frm: sender
    :param to: recepient
    :param msg: optional message - by default random one generated
    :param customTimeout:
    :return:
    """

    logger.debug("Sending msg from {} to {}".format(frm, to))
    msg = msg if msg else randomMsg()
    sender = nodes.getNode(frm)
    rid = sender.nodestack.getRemote(nodes.getNodeName(to)).uid
    sender.nodestack.send(msg, rid)

    timeout = customTimeout or waits.expectedNodeToNodeMessageDeliveryTime()

    await eventually(checkMessageReceived, msg, nodes, to, method,
                     retryWait=.1,
                     timeout=timeout,
                     ratchetSteps=10)


def sendMessageToAll(nodes: TestNodeSet,
                     frm: NodeRef,
                     msg: Optional[Tuple]=None):
    """
    Sends message from one node to all others

    :param nodes:
    :param frm: sender
    :param msg: optional message - by default random one generated
    :return:
    """
    for node in nodes:
        if node != frm:
            sendMessage(nodes, frm, node, msg)


async def sendMessageAndCheckDeliveryToAll(nodes: TestNodeSet,
                                           frm: NodeRef,
                                           msg: Optional[Tuple]=None,
                                           method=None,
                                           customTimeout=None):
    """
    Sends message from one node to all other and checks that it was delivered

    :param nodes:
    :param frm: sender
    :param msg: optional message - by default random one generated
    :param customTimeout:
    :return:
    """
    customTimeout = customTimeout or waits.expectedNodeToAllNodesMessageDeliveryTime(
        len(nodes))
    for node in nodes:
        if node != frm:
            await sendMessageAndCheckDelivery(nodes, frm, node, msg, method, customTimeout)
            break


def checkMessageReceived(msg, nodes, to, method: str = None):
    allMsgs = nodes.getAllMsgReceived(to, method)
    assert msg in allMsgs


def addNodeBack(nodeSet: TestNodeSet,
                looper: Looper,
                nodeName: str) -> TestNode:
    node = nodeSet.addNode(nodeName)
    looper.add(node)
    return node


def checkPropagateReqCountOfNode(node: TestNode, identifier: str, reqId: int):
    key = identifier, reqId
    assert key in node.requests
    assert node.quorums.propagate.is_reached(
        len(node.requests[key].propagates))


def requestReturnedToNode(node: TestNode, identifier: str, reqId: int,
                          instId: int):
    params = getAllArgs(node, node.processOrdered)
    # Skipping the view no and time from each ordered request
    recvdOrderedReqs = [
        (p['ordered'].instId, *p['ordered'].reqIdr[0]) for p in params]
    expected = (instId, identifier, reqId)
    return expected in recvdOrderedReqs


def checkRequestReturnedToNode(node: TestNode, identifier: str, reqId: int,
                               instId: int):
    assert requestReturnedToNode(node, identifier, reqId, instId)


def checkRequestNotReturnedToNode(node: TestNode, identifier: str, reqId: int,
                                  instId: int):
    assert not requestReturnedToNode(node, identifier, reqId, instId)


def check_request_is_not_returned_to_nodes(looper, nodeSet, request):
    instances = range(getNoInstances(len(nodeSet)))
    coros = []
    for node, inst_id in itertools.product(nodeSet, instances):
        c = partial(checkRequestNotReturnedToNode,
                    node=node,
                    identifier=request.identifier,
                    reqId=request.reqId,
                    instId=inst_id
                    )
        coros.append(c)
    timeout = waits.expectedTransactionExecutionTime(len(nodeSet))
    looper.run(eventuallyAll(*coros, retryWait=1, totalTimeout=timeout))


def checkPrePrepareReqSent(replica: TestReplica, req: Request):
    prePreparesSent = getAllArgs(replica, replica.sendPrePrepare)
    expectedDigest = TestReplica.batchDigest([req])
    assert expectedDigest in [p["ppReq"].digest for p in prePreparesSent]
    assert [(req.identifier, req.reqId)] in \
           [p["ppReq"].reqIdr for p in prePreparesSent]


def checkPrePrepareReqRecvd(replicas: Iterable[TestReplica],
                            expectedRequest: PrePrepare):
    for replica in replicas:
        params = getAllArgs(replica, replica.canProcessPrePrepare)
        assert expectedRequest.reqIdr in [p['pp'].reqIdr for p in params]


def checkPrepareReqSent(replica: TestReplica, identifier: str, reqId: int,
                        view_no: int):
    paramsList = getAllArgs(replica, replica.canPrepare)
    rv = getAllReturnVals(replica,
                          replica.canPrepare)
    args = [p["ppReq"].reqIdr for p in paramsList if p["ppReq"].viewNo == view_no]
    assert [(identifier, reqId)] in args
    idx = args.index([(identifier, reqId)])
    assert rv[idx]


def checkSufficientPrepareReqRecvd(replica: TestReplica, viewNo: int,
                                   ppSeqNo: int):
    key = (viewNo, ppSeqNo)
    assert key in replica.prepares
    assert len(replica.prepares[key][1]) >= replica.quorums.prepare.value


def checkSufficientCommitReqRecvd(replicas: Iterable[TestReplica], viewNo: int,
                                  ppSeqNo: int):
    for replica in replicas:
        key = (viewNo, ppSeqNo)
        assert key in replica.commits
        received = len(replica.commits[key][1])
        minimum = replica.quorums.commit.value
        assert received > minimum


def checkReqAck(client, node, idr, reqId, update: Dict[str, str] = None):
    rec = {OP_FIELD_NAME: REQACK, f.REQ_ID.nm: reqId, f.IDENTIFIER.nm: idr}
    if update:
        rec.update(update)
    expected = (rec, node.clientstack.name)
    # More than one matching message could be present in the client's inBox
    # since client on not receiving request under timeout might have retried
    # the request
    assert client.inBox.count(expected) > 0


def checkReqNack(client, node, idr, reqId, update: Dict[str, str] = None):
    rec = {OP_FIELD_NAME: REQNACK, f.REQ_ID.nm: reqId, f.IDENTIFIER.nm: idr}
    if update:
        rec.update(update)
    expected = (rec, node.clientstack.name)
    # More than one matching message could be present in the client's inBox
    # since client on not receiving request under timeout might have retried
    # the request
    assert client.inBox.count(expected) > 0


def checkReplyCount(client, idr, reqId, count):
    senders = set()
    for msg, sdr in client.inBox:
        if msg[OP_FIELD_NAME] == REPLY and \
                msg[f.RESULT.nm][f.IDENTIFIER.nm] == idr and \
                msg[f.RESULT.nm][f.REQ_ID.nm] == reqId:
            senders.add(sdr)
    assertLength(senders, count)


def wait_for_replies(looper, client, idr, reqId, count, custom_timeout=None):
    timeout = custom_timeout or waits.expectedTransactionExecutionTime(
        len(client.nodeReg))
    looper.run(eventually(checkReplyCount, client, idr, reqId, count,
                          timeout=timeout))


def checkReqNackWithReason(client, reason: str, sender: str):
    found = False
    for msg, sdr in client.inBox:
        if msg[OP_FIELD_NAME] == REQNACK and reason in msg.get(
                f.REASON.nm, "") and sdr == sender:
            found = True
            break
    assert found, "there is no Nack with reason: {}".format(reason)


def wait_negative_resp(looper, client, reason, sender, timeout, chk_method):
    return looper.run(eventually(chk_method,
                                 client,
                                 reason,
                                 sender,
                                 timeout=timeout))


def waitReqNackWithReason(looper, client, reason: str, sender: str):
    timeout = waits.expectedReqNAckQuorumTime()
    return wait_negative_resp(looper, client, reason, sender, timeout,
                              checkReqNackWithReason)


def checkRejectWithReason(client, reason: str, sender: str):
    found = False
    for msg, sdr in client.inBox:
        if msg[OP_FIELD_NAME] == REJECT and reason in msg.get(
                f.REASON.nm, "") and sdr == sender:
            found = True
            break
    assert found


def waitRejectWithReason(looper, client, reason: str, sender: str):
    timeout = waits.expectedReqRejectQuorumTime()
    return wait_negative_resp(looper, client, reason, sender, timeout,
                              checkRejectWithReason)


def ensureRejectsRecvd(looper, nodes, client, reason, timeout=5):
    for node in nodes:
        looper.run(eventually(checkRejectWithReason, client, reason,
                              node.clientstack.name, retryWait=1,
                              timeout=timeout))


def waitReqNackFromPoolWithReason(looper, nodes, client, reason):
    for node in nodes:
        waitReqNackWithReason(looper, client, reason,
                              node.clientstack.name)


def waitRejectFromPoolWithReason(looper, nodes, client, reason):
    for node in nodes:
        waitRejectWithReason(looper, client, reason,
                             node.clientstack.name)


def checkViewNoForNodes(nodes: Iterable[TestNode], expectedViewNo: int = None):
    """
    Checks if all the given nodes have the expected view no

    :param nodes: The nodes to check for
    :param expectedViewNo: the view no that the nodes are expected to have
    :return:
    """

    viewNos = set()
    for node in nodes:
        logger.debug("{}'s view no is {}".format(node, node.viewNo))
        viewNos.add(node.viewNo)
    assert len(viewNos) == 1
    vNo, = viewNos
    if expectedViewNo:
        assert vNo == expectedViewNo, ','.join(['{} -> Ratio: {}'.format(
            node.name, node.monitor.masterThroughputRatio()) for node in nodes])
    return vNo


def waitForViewChange(looper, nodeSet, expectedViewNo=None,
                      customTimeout=None):
    """
    Waits for nodes to come to same view.
    Raises exception when time is out
    """

    timeout = customTimeout or waits.expectedPoolElectionTimeout(len(nodeSet))
    return looper.run(eventually(checkViewNoForNodes,
                                 nodeSet,
                                 expectedViewNo,
                                 timeout=timeout))


def getNodeSuspicions(node: TestNode, code: int = None):
    params = getAllArgs(node, TestNode.reportSuspiciousNode)
    if params and code is not None:
        params = [param for param in params
                  if 'code' in param and param['code'] == code]
    return params


def checkDiscardMsg(processors, discardedMsg,
                    reasonRegexp, *exclude):
    if not exclude:
        exclude = []
    for p in filterNodeSet(processors, exclude):
        last = p.spylog.getLastParams(p.discard, required=False)
        assert last
        assert last['msg'] == discardedMsg
        assert reasonRegexp in last['reason']


def countDiscarded(processor, reasonPat):
    c = 0
    for entry in processor.spylog.getAll(processor.discard):
        if 'reason' in entry.params and (
            (isinstance(
                entry.params['reason'],
                str) and reasonPat in entry.params['reason']),
                (reasonPat in str(
                entry.params['reason']))):
            c += 1
    return c


def filterNodeSet(nodeSet, exclude: List[Union[str, Node]]):
    """
    Return a set of nodes with the nodes in exclude removed.

    :param nodeSet: the set of nodes
    :param exclude: the list of nodes or node names to exclude
    :return: the filtered nodeSet
    """
    return [n for n in nodeSet
            if n not in
            [nodeSet[x] if isinstance(x, str) else x for x in exclude]]


def whitelistNode(toWhitelist: str, frm: Sequence[TestNode], *codes):
    for node in frm:
        node.whitelistNode(toWhitelist, *codes)


def whitelistClient(toWhitelist: str, frm: Sequence[TestNode], *codes):
    for node in frm:
        node.whitelistClient(toWhitelist, *codes)


def assertExp(condition):
    assert condition


def assertFunc(func):
    assert func()


def checkLedgerEquality(ledger1, ledger2):
    assertLength(ledger1, ledger2.size)
    assertEquality(ledger1.root_hash, ledger2.root_hash)


def checkAllLedgersEqual(*ledgers):
    for l1, l2 in combinations(ledgers, 2):
        checkLedgerEquality(l1, l2)


def checkStateEquality(state1, state2):
    assertEquality(state1.as_dict, state2.as_dict)
    assertEquality(state1.committedHeadHash, state2.committedHeadHash)
    assertEquality(state1.committedHead, state2.committedHead)


def check_seqno_db_equality(db1, db2):
    assert db1.size == db2.size,\
        "{} != {}".format(db1.size, db2.size)
    assert {bytes(k): bytes(v) for k, v in db1._keyValueStorage.iterator()} == \
           {bytes(k): bytes(v) for k, v in db2._keyValueStorage.iterator()}


def check_last_ordered_3pc(node1, node2):
    master_replica_1 = node1.master_replica
    master_replica_2 = node2.master_replica
    assert master_replica_1.last_ordered_3pc == master_replica_2.last_ordered_3pc, \
        "{} != {}".format(master_replica_1.last_ordered_3pc,
                          master_replica_2.last_ordered_3pc)
    return master_replica_1.last_ordered_3pc


def randomText(size):
    return ''.join(random.choice(string.ascii_letters) for _ in range(size))


def mockGetInstalledDistributions(packages):
    ret = []
    for pkg in packages:
        obj = type('', (), {})()
        obj.key = pkg
        ret.append(obj)
    return ret


def mockImportModule(moduleName):
    obj = type(moduleName, (), {})()
    obj.send_message = lambda *args: None
    return obj


def initDirWithGenesisTxns(
        dirName,
        tconf,
        tdirWithPoolTxns=None,
        tdirWithDomainTxns=None,
        new_pool_txn_file=None,
        new_domain_txn_file=None):
    os.makedirs(dirName, exist_ok=True)
    if tdirWithPoolTxns:
        new_pool_txn_file = new_pool_txn_file or tconf.poolTransactionsFile
        copyfile(
            os.path.join(
                tdirWithPoolTxns, genesis_txn_file(
                    tconf.poolTransactionsFile)), os.path.join(
                dirName, genesis_txn_file(new_pool_txn_file)))
    if tdirWithDomainTxns:
        new_domain_txn_file = new_domain_txn_file or tconf.domainTransactionsFile
        copyfile(
            os.path.join(
                tdirWithDomainTxns, genesis_txn_file(
                    tconf.domainTransactionsFile)), os.path.join(
                dirName, genesis_txn_file(new_domain_txn_file)))


def stopNodes(nodes: List[TestNode], looper=None, ensurePortsFreedUp=True):
    if ensurePortsFreedUp:
        assert looper, 'Need a looper to make sure ports are freed up'

    for node in nodes:
        node.stop()

    if ensurePortsFreedUp:
        ports = [[n.nodestack.ha[1], n.clientstack.ha[1]] for n in nodes]
        waitUntilPortIsAvailable(looper, ports)


def waitUntilPortIsAvailable(looper, ports, timeout=5):
    ports = itertools.chain(*ports)

    def chk():
        for port in ports:
            checkPortAvailable(("", port))

    looper.run(eventually(chk, retryWait=.5, timeout=timeout))


def run_script(script, *args):
    s = os.path.join(os.path.dirname(__file__), '../../scripts/' + script)
    command = [executable, s]
    command.extend(args)

    with Popen([executable, s]) as p:
        sleep(4)
        p.send_signal(SIGINT)
        p.wait(timeout=1)
        assert p.poll() == 0, 'script failed'


def viewNoForNodes(nodes):
    viewNos = {node.viewNo for node in nodes}
    assert 1 == len(viewNos)
    return next(iter(viewNos))


def primaryNodeNameForInstance(nodes, instanceId):
    primaryNames = {node.replicas[instanceId].primaryName for node in nodes}
    assert 1 == len(primaryNames)
    primaryReplicaName = next(iter(primaryNames))
    return primaryReplicaName[:-2]


def nodeByName(nodes, name):
    for node in nodes:
        if node.name == name:
            return node
    raise Exception("Node with the name '{}' has not been found.".format(name))


def send_pre_prepare(view_no, pp_seq_no, wallet, nodes,
                     state_root=None, txn_root=None):
    last_req_id = wallet._getIdData().lastReqId or 0
    pre_prepare = PrePrepare(
        0,
        view_no,
        pp_seq_no,
        get_utc_epoch(),
        [(wallet.defaultId, last_req_id + 1)],
        0,
        "random digest",
        DOMAIN_LEDGER_ID,
        state_root or '0' * 44,
        txn_root or '0' * 44
    )
    primary_node = getPrimaryReplica(nodes).node
    non_primary_nodes = set(nodes) - {primary_node}

    sendMessageToAll(nodes, primary_node, pre_prepare)
    for non_primary_node in non_primary_nodes:
        sendMessageToAll(nodes, non_primary_node, pre_prepare)


def send_prepare(view_no, pp_seq_no, nodes, state_root=None, txn_root=None):
    prepare = Prepare(
        0,
        view_no,
        pp_seq_no,
        get_utc_epoch(),
        "random digest",
        state_root or '0' * 44,
        txn_root or '0' * 44
    )
    primary_node = getPrimaryReplica(nodes).node
    sendMessageToAll(nodes, primary_node, prepare)


def send_commit(view_no, pp_seq_no, nodes):
    commit = Commit(
        0,
        view_no,
        pp_seq_no)
    primary_node = getPrimaryReplica(nodes).node
    sendMessageToAll(nodes, primary_node, commit)


def chk_all_funcs(looper, funcs, acceptable_fails=0, retry_wait=None,
                  timeout=None, override_eventually_timeout=False):
    # TODO: Move this logic to eventuallyAll
    def chk():
        fails = 0
        for func in funcs:
            try:
                func()
            except Exception as ex:
                fails += 1
                if fails >= acceptable_fails:
                    logger.debug('Too many fails, the last one: {}'.format(repr(ex)))
        assert fails <= acceptable_fails

    kwargs = {}
    if retry_wait:
        kwargs['retryWait'] = retry_wait
    if timeout:
        kwargs['timeout'] = timeout
    if override_eventually_timeout:
        kwargs['override_timeout_limit'] = override_eventually_timeout

    looper.run(eventually(chk, **kwargs))
