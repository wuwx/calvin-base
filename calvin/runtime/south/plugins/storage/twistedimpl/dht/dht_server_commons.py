# -*- coding: utf-8 -*-

# Copyright (c) 2015 Ericsson AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import traceback
import random
try:
    import pydot
except:
    pydot = None
import hashlib
import OpenSSL.crypto

from calvin.utilities import calvinlogger
from calvin.runtime.south.plugins.storage.twistedimpl.dht import append_server
from calvin.runtime.south.plugins.storage.twistedimpl.dht import dht_server
from calvin.runtime.south.plugins.storage.twistedimpl.dht import service_discovery_ssdp
# FIXME refactor to calvin.runtime.north.certificate
from calvin.runtime.south.plugins.storage.twistedimpl.dht import certificate

from kademlia.node import Node
from kademlia.utils import deferredDict, digest
from kademlia.crawling import NodeSpiderCrawl

from calvin.runtime.south.plugins.async import threads
from calvin.utilities import calvinconfig

_conf = calvinconfig.get()
_log = calvinlogger.get_logger(__name__)

def generate_challenge():
    """ Generate a random challenge of 8 bytes, hex string formated"""
    return os.urandom(8).encode("hex")

class IDServerApp(object):

    def __init__(self, server_type, identifier):
        self.kserver = None
        self.port = None
        self.server_type = server_type
        self.id = identifier

    def start(self, port=0, iface='', bootstrap=None):
        if bootstrap is None:
            bootstrap = []

        self.kserver = self.server_type(id=self.id)
        self.kserver.bootstrap(bootstrap)

        self.port = service_discovery_ssdp.reactor.listenUDP(port,
                                                            self.kserver.protocol,
                                                            interface=iface)

        return self.port.getHost().host, self.port.getHost().port

    def __getattr__(self, name):
        if hasattr(self.kserver, name) and callable(getattr(self.kserver, name)):
            return getattr(self.kserver, name)
        else:
            # Default behaviour
            raise AttributeError

    def get_port(self):
        return self.port

    def stop(self):
        if self.port:
            return self.port.stopListening()

class niceAutoDHTServer(dht_server.AutoDHTServer):
    def __init__(self, *args, **kwargs):
        super(niceAutoDHTServer, self).__init__(*args, **kwargs)
        self.cert_conf = certificate.Config(_conf.get("security", "certificate_conf"),
                                            _conf.get("security", "certificate_domain")).configuration

    def start(self, iface='', network=None, bootstrap=None, cb=None, type=None, name=None):
        if bootstrap is None:
            bootstrap = []
        name_dir = os.path.join(self.cert_conf["CA_default"]["runtimes_dir"], name)
        filename = os.listdir(os.path.join(name_dir, "mine"))
        st_cert = open(os.path.join(name_dir, "mine", filename[0]), 'rt').read()
        cert_part = st_cert.split(certificate.BEGIN_LINE)
        certstr = "{}{}".format(certificate.BEGIN_LINE, cert_part[1])

        try:
            cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                  certstr)
        except:
            raise
        key = cert.digest("sha256")
        newkey = key.replace(":", "")
        bytekey = newkey.decode("hex")

        if network is None:
            network = _conf.get_in_order("dht_network_filter", "ALL")

        self.dht_server = IDServerApp(niceAppendServer, bytekey[-20:])
        ip, port = self.dht_server.start(iface=iface)

        dlist = []
        dlist.append(self.dht_server.bootstrap(bootstrap))

        self._ssdps = service_discovery_ssdp.SSDPServiceDiscovery(iface,
                                                                 cert=certstr)
        dlist += self._ssdps.start()

        _log.debug("Register service %s %s:%s" % (network, ip, port))
        self._ssdps.register_service(network, ip, port)

        _log.debug("Set client filter %s" % (network))
        self._ssdps.set_client_filter(network)

        start_cb = service_discovery_ssdp.defer.Deferred()

        def bootstrap_proxy(addrs):
            def started(args):
                _log.debug("DHT Started %s" % (args))
                if not self._started:
                    service_discovery_ssdp.reactor.callLater(.2,
                                                            start_cb.callback,
                                                            True)
                if cb:
                    service_discovery_ssdp.reactor.callLater(.2,
                                                            cb,
                                                            True)
                self._started = True

            def failed(args):
                _log.debug("DHT failed to bootstrap %s" % (args))
                #reactor.callLater(.5, bootstrap_proxy, addrs)

            _log.debug("Trying to bootstrap with %s" % (repr(addrs)))
            d = self.dht_server.bootstrap(addrs)
            d.addCallback(started)
            d.addErrback(failed)

        def start_msearch(args):
            _log.debug("** msearch %s args: %s" % (self, repr(args)))
            service_discovery_ssdp.reactor.callLater(0,
                                                    self._ssdps.start_search,
                                                    bootstrap_proxy,
                                                    stop=False)

        # Wait until servers all listen
        dl = service_discovery_ssdp.defer.DeferredList(dlist)
        dl.addBoth(start_msearch)
        #FIXME handle inside IDServerApp
        self.dht_server.kserver.protocol.sourceNode.port = port
        self.dht_server.kserver.protocol.sourceNode.ip = "0.0.0.0"
        self.dht_server.kserver.name = name
        self.dht_server.kserver.protocol.name = name
        self.dht_server.kserver.protocol.storeOwnCert(certstr)
        self.dht_server.kserver.protocol.setPrivateKey()

        return start_cb


class niceKademliaProtocolAppend(append_server.KademliaProtocolAppend):
    def __init__(self, *args, **kwargs):
        append_server.KademliaProtocol.__init__(self, *args, **kwargs)
        self.cert_conf = certificate.Config(_conf.get("security", "certificate_conf"),
                                            _conf.get("security", "certificate_domain")).configuration
        self.set_keys=set([])
        try:
            self.trustedStore = OpenSSL.crypto.X509Store() # Contains all trusted CA-certificates.
        except:
            logger(self.sourceNode, "Failed to create trustedStore")
        self.addCACert()
    #####################
    # Call Functions    #
    #####################

    def callCertFindValue(self, nodeToAsk, nodeToFind):
        """
        Asks 'nodeToAsk' for its certificate.
        """
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                     self.priv_key,
                                                     '')
            signature = OpenSSL.crypto.sign(private,
                                            nodeToAsk.id.encode("hex").upper() + challenge,
                                            "sha256")
        except:
            logger(self.sourceNode, "Signing of certFindValue failed")
            return None
        d = self.find_value(address,
                           self.sourceNode.id,
                           nodeToFind.id,
                           challenge,
                           signature,
                           self.getOwnCert())
        return d.addCallback(self.handleCertCallResponse,
                            nodeToAsk,
                            challenge)

    def callFindNode(self, nodeToAsk, nodeToFind):
        """
        Asks 'nodeToAsk' for the value 'nodeToFind.id'
        """
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                     self.priv_key,
                                                     '')
            signature = OpenSSL.crypto.sign(private,
                                           nodeToAsk.id.encode("hex").upper() + challenge,
                                           "sha256")
        except:
            logger(self.sourceNode, "Signing of findNode failed")
            return None
        d = self.find_node(address,
                          self.sourceNode.id,
                          nodeToFind.id,
                          challenge,
                          signature)
        return d.addCallback(self.handleSignedBucketResponse,
                            nodeToAsk,
                            challenge)

    def callFindValue(self, nodeToAsk, nodeToFind):
        """
        Asks 'nodeToAsk' for the information regarding the node 'nodeToFind'
        """
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                    self.priv_key,
                                                    '')
            signature = OpenSSL.crypto.sign(private,
                                           nodeToAsk.id.encode("hex").upper() + challenge,
                                           "sha256")
        except:
            logger(self.sourceNode, "Signing of findValue failed")
            return None
        d = self.find_value(address,
                           self.sourceNode.id,
                           nodeToFind.id,
                           challenge,
                           signature)
        return d.addCallback(self.handleSignedValueResponse,
                            nodeToAsk,
                            challenge)

    def callPing(self, nodeToAsk, cert=None):
        """
        Sends a ping message to 'nodeToAsk'
        """        
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                    self.priv_key,
                                                    '')
            signature = OpenSSL.crypto.sign(private,
                                           nodeToAsk.id.encode("hex").upper() + challenge,
                                           "sha256")
        except:
            logger(self.sourceNode, "Signing of ping failed")
            return None
        d = self.ping(address,
                     self.sourceNode.id,
                     challenge,
                     signature,
                     cert)
        return d.addCallback(self.handleSignedPingResponse,
                            nodeToAsk,
                            challenge)

    def callStore(self, nodeToAsk, key, value):
        """
        Sends a request for 'nodeToAsk' to store value 'value' with key 'key'
        """   
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                    self.priv_key,
                                                    '')
            signature = OpenSSL.crypto.sign(private,
                                           nodeToAsk.id.encode("hex").upper() + challenge,
                                           "sha256")
        except:
            logger(self.sourceNode, "Signing of store failed")
            return None
        d = self.store(address,
                      self.sourceNode.id,
                      key,
                      value,
                      challenge,
                      signature)
        return d.addCallback(self.handleSignedStoreResponse,
                            nodeToAsk,
                            challenge)

    #####################
    # Response handlers #
    #####################

    def handleCertCallResponse(self, result, node, challenge):
        """
        Handle Certificate responses. `result` is a response value array
        Element 0 contains ??, Element 1 is a dictionary that contains a
        certificate.

        `node` is responding node and `challenge` is the returned
        challenge value.
        Raise ?? exceptions at ?? occation.
        Return results if signatures are valid?
        """
        if 'value' in result[1]:
            try:
                cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                      result[1]['value'])
            except:
                logger(self.sourceNode, "Invalid certificate "
                                        "response from {}".format(node))
                return None
            fingerprint = cert.digest("sha256")
            id = fingerprint.replace(":","")[-40:]
            if node.id.encode('hex').upper() == id:
                try:
                    OpenSSL.crypto.verify(cert,
                                         result[1]['signature'],
                                         challenge,
                                         "sha256")
                except:
                    logger(self.sourceNode,
                          "Invalid signature on certificate "
                          "response from {}".format(node))
                self.router.addContact(node)
                self.storeCert(result[1]['value'], id)
                if self.router.isNewNode(node):
                    self.transferKeyValues(node)
            else:
                # logger(self.sourceNode, "Certificate from {} does not match claimed node id".format(node))
                return None
        else:
            self.router.removeContact(node)
        return result

    def handleSignedBucketResponse(self, result, node, challenge):
        """
        ???
        `result` is an array and element 1 contains a dict.
        Return None if any error occur and dont tell anyone.
        """
        nodeIdHex = node.id.encode('hex').upper()
        if result[0]:
            if "NACK" in result[1]:
                return self.handleSignedNACKResponse(result, node, challenge)
            elif 'bucket' in result[1] and 'signature' in result[1]:
                cert_stored = self.searchForCertificate(nodeIdHex)
                if cert_stored == None:
                    logger(self.sourceNode,
                           "Certificate for sender of bucket:"
                           " {} not present in store".format(node))
                    return None
                try:
                    OpenSSL.crypto.verify(cert_stored,
                                         result[1]['signature'],
                                         challenge,
                                         "sha256")
                    self.router.addContact(node)
                    newbucket = list()
                    for bucketnode in result[1]['bucket']:
                        buId = bucketnode[0]
                        buIdHex = buId.encode('hex').upper()
                        buIp = bucketnode[1]
                        buPort = bucketnode[2]
                        if not self.certificateExists(buIdHex):
                            nonCertifiedNode = Node(buId,
                                                    buIp,
                                                    buPort)
                            buIdDigest = digest(str(buIdHex))
                            buIdReq = "{}cert".format(Node(buIdDigest))
                            self.callCertFindValue(nonCertifiedNode,
                                                  buIdReq)
                        else:
                            newbucket.append(bucketnode)
                    return (result[0], newbucket)
                except:
                    logger(self.sourceNode,
                          "Bad signature for"
                          " sender of bucket: {}".format(node))
                    return None
            else:
                if not result[1]['signature']:
                    logger(self.sourceNode,
                          "Signature not present"
                          " for sender of bucket: {}".format(node))
                return None
        else:
            logger(self.sourceNode,
                  "No response from {},"
                  " removing from bucket".format(node))
            self.router.removeContact(node)
        return None

    def handleSignedPingResponse(self, result, node, challenge):
        """
        Handler for signed ping responses.
        `result` contains an array
            Element 0 contains ??
            Element 1 contains a dict with message fields.
        Return None on error.
        Return identity of ping response if signature is valid.
        """
        if result[0]:
            if "NACK" in result[1]:
                return self.handleSignedNACKResponse(result,
                                                    node,
                                                    challenge)
            elif 'id' in result[1] and 'signature' in result[1]:
                if result[1]['id'] != node.id:
                    logger(self.sourceNode,
                          "Pong ID return "
                          "mismatch for {}".format(node))
                    return None
                nodeIdHex = node.id.encode('hex').upper()
                cert_stored = self.searchForCertificate(nodeIdHex)
                if cert_stored == None:
                    logger(self.sourceNode,
                          "Certificate for sender of pong: {} "
                          "not present in store".format(node))
                    return None
                try: 
                    OpenSSL.crypto.verify(cert_stored,
                                         result[1]['signature'],
                                         challenge,
                                         "sha256")
                    self.router.addContact(node)
                    return result[1]['id']
                except:
                    logger(self.sourceNode,
                          "Bad signature for sender"
                          " of pong: {}".format(node))
                    return None
            else:
                logger(self.sourceNode,
                      "Signature not present for sender"
                      " of pong: {}".format(node))
                return None
        else:
            logger(self.sourceNode,
                  "No pong from {}, removing"
                  " from bucket".format(node))
            self.router.removeContact(node)
        return None

    def handleSignedStoreResponse(self, result, node, challenge):
        if result[0]:
            if "NACK" in result[1]:
                return self.handleSignedNACKResponse(result,
                                                    node,
                                                    challenge)
            nodeIdHex = node.id.encode('hex').upper()
            cert_stored = self.searchForCertificate(nodeIdHex)
            if cert_stored == None:
                logger(self.sourceNode,
                "Certificate for sender of store confirmation: {}"
                " not present in store".format(node))
                return None
            try: 
                OpenSSL.crypto.verify(cert_stored,
                                     result[1],
                                     challenge,
                                     "sha256")
                self.router.addContact(node)
                return (True, True)
            except:
                logger(self.sourceNode,
                      "Bad signature for sender of store"
                      " confirmation: {}".format(node))
                return None
        else:
            logger(self.sourceNode,
                  "No store confirmation from {},"
                  " removing from bucket".format(node))
            self.router.removeContact(node)
        return None

    def handleSignedValueResponse(self, result, node, challenge):
        if result[0]:
            if "NACK" in result[1]:
                return self.handleSignedNACKResponse(result,
                                                    node,
                                                    challenge)
            elif 'bucket' in result[1]:
                return self.handleSignedBucketResponse(result,
                                                      node,
                                                      challenge)
            elif 'value' in result[1] and 'signature' in result[1]:
                nodeIdHex = node.id.encode('hex').upper()
                cert_stored = self.searchForCertificate(nodeIdHex)
                if cert_stored == None:
                    logger(self.sourceNode,
                          "Certificate for sender of value response: {}"
                          " not present in store".format(node))
                    return None
                try: 
                    OpenSSL.crypto.verify(cert_stored,
                                         result[1]['signature'],
                                         challenge,
                                         "sha256")
                    self.router.addContact(node)
                    return result
                except:
                    logger(self.sourceNode,
                          "Bad signature for sender of "
                          "value response: {}".format(node))
                    return None
            else:
                logger(self.sourceNode,
                      "Signature not present for sender "
                      "of value response: {}".format(node))
                return None
        else:
            logger(self.sourceNode,
                  "No value response from {}, "
                  "removing from bucket".format(node))
            self.router.removeContact(node)
        return None

    def handleSignedNACKResponse(self, result, node, challenge):
        nodeIdHex = node.id.encode('hex').upper()
        cert_stored = self.searchForCertificate(nodeIdHex)
        if cert_stored == None:
            logger(self.sourceNode,
                  "Certificate for sender of NACK: {} "
                  "not present in store".format(node))
        if "NACK" in result[1]:
            logger(self.sourceNode,
                  "NACK in Value response")
            try:
                OpenSSL.crypto.verify(cert_stored,
                                     result[1]['signature'],
                                     challenge,
                                     "sha256")
                self.callPing(node, self.getOwnCert())
                logger(self.sourceNode, "Certificate sent!")
            except:
                logger(self.sourceNode,
                      "Bad signature for sender "
                      "of NACK: {}".format(node))
        return None


    #####################
    # RPC Functions     #
    #####################

    def rpc_store(self, sender, nodeid, key, value, challenge, signature):
        source = Node(nodeid, sender[0], sender[1])
        nodeIdHex = nodeid.encode('hex').upper()
        cert_stored = self.searchForCertificate(nodeIdHex)
        if cert_stored == None:
            try:
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                      self.priv_key,
                                                      '')
                signature = OpenSSL.crypto.sign(private,
                                               challenge,
                                               "sha256")
            except:
                return None
            logger(self.sourceNode,
                  "Certificate for {} not "
                  "found in store".format(source))
            return {'NACK' : None, "signature" : signature}
        else:
            try:
                sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                payload = "{}{}".format(sourceNodeIdHex, challenge)
                OpenSSL.crypto.verify(cert_stored,
                                     signature,
                                     payload,
                                     "sha256")
            except:
                logger(self.sourceNode,
                      "Bad signature for sender of "
                      "store request: {}".format(source))
                return None
            self.router.addContact(source)
            self.storage[key] = value
            try:
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                        self.priv_key,
                                                        '')
                signature = OpenSSL.crypto.sign(private,
                                               challenge,
                                               "sha256")
            except:
                logger(self.sourceNode,
                      "Signing of rpc_store failed")
                return None
            return signature

    def rpc_find_node(self, sender, nodeid, key, challenge, signature):
        nodeIdHex = nodeid.encode('hex').upper()
        logger(self.sourceNode,
              "finding neighbors of {} "
              "in local table".format(long(nodeIdHex, 16)))

        source = Node(nodeid, sender[0], sender[1])
        cert_stored = self.searchForCertificate(nodeIdHex)
        if cert_stored == None:
            try:
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                      self.priv_key,
                                                      '')
                signature = OpenSSL.crypto.sign(private,
                                               challenge,
                                               "sha256")
            except:
                return None
            logger(self.sourceNode,
                  "Certificate for {} not found "
                  "in store".format(source))
            return {'NACK' : None, "signature" : signature}
        else:
            try:
                sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                payload = "{}{}".format(sourceNodeIdHex, challenge)
                OpenSSL.crypto.verify(cert_stored,
                                     signature,
                                     payload,
                                     "sha256")
            except:
                logger(self.sourceNode,
                      "Bad signature for sender of "
                      "find_node: {}".format(source))
                return None
            self.router.addContact(source)
            node = Node(key)
            bucket = map(list, self.router.findNeighbors(node, exclude=source))
            try:
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                        self.priv_key,
                                                        '')
                signature = OpenSSL.crypto.sign(private,
                                               challenge,
                                               "sha256")
            except:
                logger(self.sourceNode,
                      "Signing of rpc_find_node failed")
                return None
            value = {'bucket': bucket, 'signature': signature}
            return value

    def rpc_find_value(self, sender, nodeid, key, challenge, signature, certString=None):
        """
        ???
        Verifying received `challenge` and `signature` using
        supplied signature or stored signature derived from `nodeid`.
        """
        source = Node(nodeid, sender[0], sender[1])
        nodeIdHex = nodeid.encode('hex').upper()
        cert_stored = self.searchForCertificate(nodeIdHex)
        if cert_stored == None:
            sourceNodeIdHex = self.sourceNode.id.encode("hex").upper()

            if key == digest("{}cert".format(sourceNodeIdHex)) and \
                                                        certString != None:
            # If the senders certificate is not in store,
            # the only allowed action is to ask it for its certificate
                try:
                    cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                          certString)
                    store_ctx = OpenSSL.crypto.X509StoreContext(self.trustedStore,
                                                               cert)
                    store_ctx.verify_certificate()
                    # Ensure that the CA of the received certificate is trusted

                    fingerprint = cert.digest("sha256")
                    id = fingerprint.replace(":", "")[-40:]
                    if id != nodeIdHex:
                        logger(self.sourceNode,
                              "Explicit certificate in find_value "
                              "from {} does not match nodeid".format(source))
                        return None
                    sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                    payload = "{}{}".format(sourceNodeIdHex, challenge)
                    OpenSSL.crypto.verify(cert,
                                         signature,
                                         payload,
                                         "sha256")
                    self.storeCert(certString, nodeIdHex)
                except:
                    logger(self.sourceNode,
                          "Invalid certificate "
                          "request: {}".format(source))
                    return None
            else:
                try:
                    private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                            self.priv_key,
                                                            '')
                    signature = OpenSSL.crypto.sign(private,
                                    challenge,
                                    "sha256")
                except:
                    return None
                logger(self.sourceNode,
                      "Certificate for {} not "
                      "found in store".format(source))
                return { 'NACK' : None, 'signature': signature}
        else:
            try:
                sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                payload = "{}{}".format(sourceNodeIdHex, challenge)
                # Verifying stored certificate with signature.
                OpenSSL.crypto.verify(cert_stored,
                                     signature,
                                     payload,
                                     "sha256")
            except:
                logger(self.sourceNode,
                      "Bad signature for sender of "
                      "find_value: {}".format(source))
                return None

        self.router.addContact(source)
        exists, value = self.storage.get(key, None)
        if not exists:
            logger(self.sourceNode,
                  "Key {} not in store, forwarding").format(key)
            return self.rpc_find_node(sender,
                                     nodeid,
                                     key,
                                     challenge,
                                     signature)
        else:
            try:
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                        self.priv_key,
                                                        '')
                signature = OpenSSL.crypto.sign(private,
                                               challenge,
                                               "sha256")
            except:
                logger(self.sourceNode,
                      "Signing of rpc_find_value failed")
                return None
            return { 'value': value, 'signature': signature }

    def rpc_ping(self, sender, nodeid, challenge, signature, certString=None):
        """
        This function is ???
        Verify `certString` certificate with CA from trust store.
        Verify `signature` of `challenge`.
        Store certificate if `certString` is verified.

        """
        source = Node(nodeid, sender[0], sender[1])
        nodeIdHex = nodeid.encode("hex").upper()
        if certString != None:
            try:
                cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                      certString)
                store_ctx = OpenSSL.crypto.X509StoreContext(self.trustedStore,
                                                           cert)
                store_ctx.verify_certificate()
                # Ensure that the CA of the received certificate is trusted
                fingerprint = cert.digest("sha256")
                id = fingerprint.replace(":", "")[-40:]
                if id != nodeIdHex:
                    logger(self.sourceNode,
                          "Explicit certificate in ping from {} "
                          "does not match nodeid".format(source))
                    return None
                sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                payload = "{}{}".format(sourceNodeIdHex, challenge)
                OpenSSL.crypto.verify(cert,
                                     signature,
                                     payload,
                                     "sha256")
                if not self.certificateExists(nodeIdHex):
                    self.storeCert(certString, nodeIdHex)
                    self.transferKeyValues(source)
            except:
                logger(self.sourceNode,
                      "Bad signature for sender of ping with "
                      "explicit certificate: {}".format(source))
                return None
        else:
            cert_stored = self.searchForCertificate(nodeIdHex)
            if cert_stored == None:
                try:
                    private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                          self.priv_key,
                                                          '')
                    signature = OpenSSL.crypto.sign(private,
                                                   challenge,
                                                   "sha256")
                except:
                    return None
                logger(self.sourceNode,
                      "Certificate for {} not found "
                      "in store".format(source))
                return {'NACK' : None, "signature" : signature}
            else:
                try:
                    sourceNodeIdHex = self.sourceNode.id.encode('hex').upper()
                    payload = "{}{}".format(sourceNodeIdHex, challenge)
                    OpenSSL.crypto.verify(cert_stored,
                                         signature,
                                         payload,
                                         "sha256")
                except:
                    logger(self.sourceNode,
                          "Bad signature for sender of "
                          "ping: {}".format(source))
                    return None
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                    self.priv_key,
                                                    '')
            signature = OpenSSL.crypto.sign(private,
                                           challenge,
                                           "sha256")
        except:
            logger(self.sourceNode, "Signing of rpc_ping failed")
            return None
        return { 'id': self.sourceNode.id, 'signature': signature }

    #####################
    # MISC              #
    #####################
    def certificateExists(self, id):
        """
        Returns however the certificate for a
        given id exists in the own DHT storage.
        """
        return digest("{}cert".format(id)) in self.storage

    def searchForCertificate(self, id):
        """
        Seaches the internal storage for the certificate
        for a node with a given ID. If only one certificate
        is found to match the ID, this is returned.
        If none or several is found, None is returned.
        """
        if digest("{}cert".format(id)) in self.storage:
            data = self.storage.get(digest("{}cert".format(id)))
            try:
                return OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                      data[1])
            except:
                return None
        name_dir = os.path.join(self.cert_conf["CA_default"]["runtimes_dir"], self.name)
        filename = os.listdir(os.path.join(name_dir, "others"))
        matching = [s for s in filename if id in s]
        if len(matching) == 1:
            file = open(os.path.join(name_dir, "others", matching[0]), 'rt')
            st_cert = file.read()
            try:
                cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                    st_cert)
            except:
                logger(self.sourceNode,
                      "Loading error for certificate "
                      "with id: {}".format(id))
                return None
            file.close()
            return cert
        else:
            return None

    def setPrivateKey(self):
        '''
        Retrieves the nodes private key from disk and
        stores it at priv_key.
        '''
        name_dir = os.path.join(self.cert_conf["CA_default"]["runtimes_dir"], self.name)
        file = open(os.path.join(name_dir, "private", "private.key"), 'rt')
        self.priv_key = file.read()

    def addCACert(self):
        """
        Collects the CA-certificate from disk and adds
        it to the trustedStore.
        """
        try:
            # Read CA's cacert.pem file
            cafile = open(self.cert_conf["CA_default"]["certificate"], 'rt')
            cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                  cafile.read())
            self.trustedStore.add_cert(cert)
        except:
            logger(self.sourceNode, "Failed to load CA-cert")

    def _timeout(self, msgID):
        self._outstanding[msgID][0].callback((False, None))
        del self._outstanding[msgID]

    def transferKeyValues(self, node):
        """
        Given a new node, send it all the keys/values it
        should be storing. @param node: A new node that
        just joined (or that we just found out about).
        Process:
        For each key in storage, get k closest nodes.
        If newnode is closer than the furtherst in that
        list, and the node for this server is closer than
        the closest in that list, then store the key/value
        on the new node (per section 2.5 of the paper)
        """
        logger(self.sourceNode, "**** transfer key values ****")
        ds = []
        for key, value in self.storage.iteritems():
            keynode = Node(digest(key))
            neighbors = self.router.findNeighbors(keynode)
            if len(neighbors) > 0:
                newNodeClose = node.distanceTo(keynode) < neighbors[-1].distanceTo(keynode)
                thisNodeClosest = self.sourceNode.distanceTo(keynode) < neighbors[0].distanceTo(keynode)
            if len(neighbors) == 0 or (newNodeClose and thisNodeClosest):
                if key in self.set_keys:
                    ds.append(self.callAppend(node, key, value))
                else:
                    # ds.append(self.callStore(node, key, value))
                    self.callStore(node, key, value)
                    return None
        return service_discovery_ssdp.defer.gatherResults(ds)

    def storeOwnCert(self, cert):
        """
        Stores the string representation of the nodes own
        certificate in the DHT.
        """
        sourceNodeIdHex = self.sourceNode.id.encode("hex").upper()
        self.storage[digest("{}cert".format(sourceNodeIdHex))] = cert

    def storeCert(self, certString, id):
        """
        Takes a string representation of a PEM-encoded certificate and
        a nodeid as input. If the string is a valid PEM-encoded certificate
        and the CA of the the certificate is present in the trustedStore of
        this node, the certificate is stored in the DHT and written to disk
        for later use.
        """
        try:
            cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                  certString)
            store_ctx = OpenSSL.crypto.X509StoreContext(self.trustedStore,
                                                       cert)
            store_ctx.verify_certificate()
        except:
            logger(self.sourceNode,
                  "The certificate for {} is not signed "
                  "by a trusted CA!".format(id))
            return
        exists = self.storage.get(digest("{}cert".format(id)))
        if not exists[0]:
            self.storage[digest("{}cert".format(id))] = certString
            name_dir = os.path.join(self.cert_conf["CA_default"]["runtimes_dir"], self.name)
            file = open(os.path.join(name_dir, "others", "{}.pem".format(id)), 'w')
            file.write(certString)
            file.close()

    def getOwnCert(self):
        """
        Retrieves the nodes own certificate from the nodes DHT-storage and
        returns it.
        """
        sourceNodeIdHex = self.sourceNode.id.encode("hex").upper()
        return self.storage[digest("{}cert".format(sourceNodeIdHex))]


class niceAppendServer(append_server.AppendServer):
    def __init__(self, ksize=5, alpha=3, id=None, storage=None):
        storage = storage or append_server.ForgetfulStorageFix()
        append_server.Server.__init__(self,
                                     ksize,
                                     alpha,
                                     id,
                                     storage=storage)
        self.protocol = niceKademliaProtocolAppend(self.node,
                                                  self.storage,
                                                  ksize)

    def bootstrap(self, addrs):
        """
        Bootstrap the server by connecting to other known nodes in the network.
        Args:
            addrs: A `list` of (ip, port) `tuple` pairs.  Note that only IP addresses
                   are acceptable - hostnames will cause an error.
        """
        # if the transport hasn't been initialized yet, wait a second
        if self.protocol.transport is None:
            return append_server.task.deferLater(service_discovery_ssdp.reactor,
                                                1,
                                                self.bootstrap,
                                                addrs)

        def initTable(results, challenge, id):
            nodes = []
            for addr, result in results.items():
                ip = addr[0]
                port = addr[1]
                if result[0]:
                    resultSign = result[1]['signature']
                    resultId = result[1]['id']
                    resultIdHex = resultId.encode('hex').upper()
                    data = self.protocol.certificateExists(resultIdHex)
                    if not data:
                        identifier = digest("{}cert".format(resultIdHex))
                        self.protocol.callCertFindValue(Node(resultId,
                                                            ip,
                                                            port),
                                                       Node(identifier))
                    else:
                        cert_stored = self.protocol.searchForCertificate(resultIdHex)
                        try:
                            OpenSSL.crypto.verify(cert_stored,
                                                 resultSign,
                                                 challenge,
                                                 "sha256")
                        except:
                            traceback.print_exc()
                        nodes.append(Node(resultId,
                                         ip,
                                         port))
            spider = NodeSpiderCrawl(self.protocol,
                                    self.node,
                                    nodes,
                                    self.ksize,
                                    self.alpha)
            return spider.find()

        ds = {}
        challenge = generate_challenge()
        id = None
        if addrs:
            data = addrs[0]
            addr = (data[0], data[1])
            try:
                cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                      data[2])
                fingerprint = cert.digest("sha256")
                id = fingerprint.replace(":", "")[-40:]
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                      self.protocol.priv_key,
                                                      '')
                signature = OpenSSL.crypto.sign(private,
                                               "{}{}".format(id, challenge),
                                               "sha256")
                ds[addr] = self.protocol.ping(addr,
                                             self.node.id,
                                             challenge,
                                             signature,
                                             self.protocol.getOwnCert())
            except:
                logger(self.protocol.sourceNode, "Certificate creation failed")
            self.protocol.storeCert(data[2], id)
            node = Node(id.decode("hex"), data[0], data[1])
            if self.protocol.router.isNewNode(node):
                return deferredDict(ds).addCallback(initTable,
                                                   challenge,
                                                   id)
        return deferredDict(ds)

class evilAutoDHTServer(dht_server.AutoDHTServer):

    def __init__(self, *args, **kwargs):
        super(evilAutoDHTServer, self).__init__(*args, **kwargs)
        self.cert_conf = certificate.Config(_conf.get("security", "certificate_conf"),
                                            _conf.get("security", "certificate_domain")).configuration

    def start(self, iface='', network=None, bootstrap=None, cb=None, type=None, name=None):
        if bootstrap is None:
            bootstrap = []
        name_dir = os.path.join(self.cert_conf["CA_default"]["runtimes_dir"], name)
        filename = os.listdir(os.path.join(name_dir, "mine"))
        st_cert = open(os.path.join(name_dir, "mine", filename[0]), 'rt').read()
        cert_part = st_cert.split(certificate.BEGIN_LINE)
        certstr = "{}{}".format(certificate.BEGIN_LINE, cert_part[1])
        try:
            cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                  certstr)
        except:
            logger(self.sourceNode,
                  "Certificate creating failed at startup")
        key = cert.digest("sha256")
        newkey = key.replace(":", "")
        bytekey = newkey.decode("hex")

        if network is None:
            network = _conf.get_in_order("dht_network_filter", "ALL")

        if network is None:
            network = _conf.get_in_order("dht_network_filter", "ALL")
        self.dht_server = IDServerApp(evilAppendServer, bytekey[-20:])
        ip, port = self.dht_server.start(iface=iface)


        dlist = []
        dlist.append(self.dht_server.bootstrap(bootstrap))

        self._ssdps = service_discovery_ssdp.SSDPServiceDiscovery(iface,
                                                                 cert=certstr)
        dlist += self._ssdps.start()

        _log.debug("Register service %s %s:%s" % (network, ip, port))
        self._ssdps.register_service(network, ip, port)

        _log.debug("Set client filter %s" % (network))
        self._ssdps.set_client_filter(network)

        start_cb = service_discovery_ssdp.defer.Deferred()

        def bootstrap_proxy(addrs):
            def started(args):
                _log.debug("DHT Started %s" % (args))
                if not self._started:
                    service_discovery_ssdp.reactor.callLater(.2,
                                                            start_cb.callback,
                                                            True)
                if cb:
                    service_discovery_ssdp.reactor.callLater(.2,
                                                            cb,
                                                            True)
                self._started = True

            def failed(args):
                _log.debug("DHT failed to bootstrap %s" % (args))
                #reactor.callLater(.5, bootstrap_proxy, addrs)

            _log.debug("Trying to bootstrap with %s" % (repr(addrs)))
            d = self.dht_server.bootstrap(addrs)
            d.addCallback(started)
            d.addErrback(failed)

        def start_msearch(args):
            _log.debug("** msearch %s args: %s" % (self, repr(args)))
            service_discovery_ssdp.reactor.callLater(0,
                                                    self._ssdps.start_search,
                                                    bootstrap_proxy,
                                                    stop=False)

        # Wait until servers all listen
        dl = service_discovery_ssdp.defer.DeferredList(dlist)
        dl.addBoth(start_msearch)
        self.dht_server.kserver.protocol.evilType = type
        self.dht_server.kserver.protocol.sourceNode.port = port
        self.dht_server.kserver.protocol.sourceNode.ip = "0.0.0.0"
        self.dht_server.kserver.name = name
        self.dht_server.kserver.protocol.name = name
        self.dht_server.kserver.protocol.storeOwnCert(certstr)
        self.dht_server.kserver.protocol.setPrivateKey()

        return start_cb



class evilKademliaProtocolAppend(niceKademliaProtocolAppend):
    def _timeout(self, msgID):
        self._outstanding[msgID][0].callback((False, None))
        del self._outstanding[msgID]

    def callPing(self, nodeToAsk, id=None):
        address = (nodeToAsk.ip, nodeToAsk.port)
        challenge = generate_challenge()
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                    self.priv_key,
                                                    '')
            signature = OpenSSL.crypto.sign(private,
                                           challenge,
                                           "sha256")
        except:
            "Signing ping failed"
        if id:
            decider = random.random()
            if decider < 0.5:
                self.ping(address, id, challenge, signature, self.getOwnCert())
            else:
                self.ping(address, id, challenge, signature)
        else:
                d = self.ping(address, self.sourceNode.id, challenge, signature, self.getOwnCert())
        return True

    def turn_evil(self, evilPort):
        old_ping = self.rpc_ping
        old_find_node = self.rpc_find_node
        old_find_value = self.rpc_find_value
        self.router.node.port = evilPort;
        if self.evilType == "poison":
            self.rpc_find_node = self.poison_rpc_find_node
            self.rpc_find_value = self.poison_rpc_find_value
            self.false_neighbour_list = []
            for i in range(0, 30):
                fakeid = hashlib.sha1(str(random.getrandbits(255))).digest()
                fake_neighbour = [fakeid,
                                 '10.0.0.9',
                                 self.router.node.port]
                self.false_neighbour_list.append(fake_neighbour)
            _log.debug("Node with port {} prepared to execute "
                       "poisoning attack".format(self.router.node.port))
        elif self.evilType == "insert":
            self.rpc_find_node = self.sybil_rpc_find_node
            self.rpc_find_value = self.poison_rpc_find_value
            ends = bytearray([0x01, 0x02, 0x03])
            self.false_neighbour_list = []
            for i in range(0, 9):
                if i < 3:
                    key = digest("APA")
                elif i > 5:
                    key = digest("KANIN")
                else:
                    key = digest("KOALA")
                key = key[:-1] + bytes(ends[i % 3])
                self.false_neighbour_list.append((key,
                                                 '10.0.0.9',
                                                 self.router.node.port))
            _log.debug("Node with port {} prepared to execute node "
                       "insertion attack".format(self.router.node.port))
        elif self.evilType == "eclipse":
            self.rpc_find_node = self.eclipse_rpc_find_node
            self.rpc_find_value = self.eclipse_rpc_find_value
            self.closest_neighbour = map(list,
                                        self.router.findNeighbors((self.router.node)))
            self.false_neighbour_list = []
            for i in range(0, 10):
                fakeid = hashlib.sha1(str(random.getrandbits(255))).digest()

                self.false_neighbour_list.append((fakeid,
                                                 '10.0.0.9',
                                                 self.router.node.port))
            _log.debug("Node with port {} prepared to execute eclipse "
                       "attack on {}".format(self.router.node.port,
                                            self.closest_neighbour[0][2]))
        elif self.evilType == "sybil":
            self.rpc_find_node = self.sybil_rpc_find_node
            self.rpc_find_value = self.poison_rpc_find_value
            self.false_neighbour_list = []
            for i in range(0, 30):
                fakeid = [hashlib.sha1(str(random.getrandbits(255))).digest()]
                fake_neighbour = [fakeid, '10.0.0.9', self.router.node.port]
                self.false_neighbour_list.append(fake_neighbour)
            _log.debug("Node with port {} prepared to execute "
                       "Sybil attack".format(self.router.node.port))

    def poison_routing_tables(self):
        fakeid = hashlib.sha1(str(random.getrandbits(255))).digest()
        self.neighbours = map(list, self.router.findNeighbors(Node(fakeid),
                                                             k=20))
        my_randoms = random.sample(xrange(len(self.neighbours)), 1)
        for nodeToAttack in my_randoms: 
            for nodeToImpersonate in range(0, len(self.neighbours)):
                if nodeToImpersonate != nodeToAttack:
                    node = Node(self.neighbours[nodeToAttack][0],
                               self.neighbours[nodeToAttack][1],
                               self.neighbours[nodeToAttack][2])
                    self.callPing(node, self.neighbours[nodeToImpersonate][0])


    # def eclipse(self):
    #     self.neighbours = map(list, self.router.findNeighbors(Node(hashlib.sha1(str(random.getrandbits(255))).digest()),k=20))
    #     for nodeToAttack in range(0, len(self.neighbours)):
    #         self.ping((self.neighbours[nodeToAttack][1], self.neighbours[nodeToAttack][2]), self.closest_neighbour[0][0], self.getOwnCert())
    #         self.ping((self.closest_neighbour[0][1], self.closest_neighbour[0][2]), self.neighbours[nodeToAttack][0], self.getOwnCert())

    # def sybil(self, node):
    #     self.ping((node[0], node[1]), hashlib.sha1(str(random.getrandbits(255))).digest(), self.getOwnCert())

    # def sybil_rpc_find_node(self, sender, nodeid, key, challenge, signature):
    #     self.log.info("finding neighbors of %i in local table" % long(nodeid.encode('hex'), 16))
    #     source = Node(nodeid, sender[0], sender[1])
    #     self.maybeTransferKeyValues(source)
    #     self.router.addContact(source)
    #     self.false_neighbour_list = random.sample(self.false_neighbour_list, len(self.false_neighbour_list))
    #     return self.false_neighbour_list

    # def eclipse_rpc_find_node(self, sender, nodeid, key, challenge, signature):
    #     self.log.info("finding neighbors of %i in local table" % long(nodeid.encode('hex'), 16))
    #     source = Node(nodeid, sender[0], sender[1])
    #     _log.debug("eclipse rpc_find_node sender=%s, source=%s, key=%s" % (sender, source, base64.b64encode(key)))
    #     self.maybeTransferKeyValues(source)
    #     self.router.addContact(source)
    #     node = Node(key)
    #     decider = random.random()
    #     if decider < 0.1:
    #         self.eclipse()
    #     self.neighbours = map(list, self.router.findNeighbors(Node(hashlib.sha1(str(random.getrandbits(255))).digest()),k=20))
    #     neighbourList = list(self.neighbours)
    #     if long(nodeid.encode('hex'), 16) != long(self.closest_neighbour[0][0].encode('hex'), 16):
    #         for i in range(0, len(neighbourList)):
    #             if neighbourList[i][0] is self.closest_neighbour[0][0]:
    #                 neighbourList[i] = (neighbourList[i][0], neighbourList[i][1], self.router.node.port)
    #     else:
    #         for i in range(0, len(neighbourList)):
    #             neighbourList[i] = (neighbourList[i][0], neighbourList[i][1], self.router.node.port)
    #     mergedlist = []
    #     mergedlist.extend(neighbourList)
    #     mergedlist.extend(self.false_neighbour_list)
    #     self.neighbours = random.sample(mergedlist, len(mergedlist))
    #     self.neighbours = list(mergedlist)
    #     return self.neighbours

    def poison_rpc_find_node(self, sender, nodeid, key, challenge, signature):
        source = Node(nodeid, sender[0], sender[1])
        # self.maybeTransferKeyValues(source)
        self.router.addContact(source)
        node = Node(key)
        decider = random.random()
        fakeid = hashlib.sha1(str(random.getrandbits(255))).digest()
        self.neighbours = map(list, self.router.findNeighbors(Node(fakeid),k=20))
        if decider < 0.1:
            self.poison_routing_tables()
        elif decider > 0.95:
            fakeid1 = hashlib.sha1(str(random.getrandbits(255))).digest()
            fakeid2 = hashlib.sha1(str(random.getrandbits(255))).digest()
            self.find_value((self.neighbours[0][1], self.neighbours[0][2]),
                           fakeid1,
                           fakeid2,
                           challenge,
                           signature)
        elif decider > 0.9:
            self.find_node((self.neighbours[0][1], self.neighbours[0][2]),
                          nodeid,
                          self.neighbours[0][0],
                          challenge,
                          signature)
        neighbourList = list(self.neighbours)
        for i in range(0, len(neighbourList)):
            neighbourList[i] = [neighbourList[i][0],
                               neighbourList[i][1],
                               self.router.node.port]
        mergedlist = []
        mergedlist.extend(neighbourList)
        mergedlist.extend(self.false_neighbour_list)
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                  self.priv_key,
                                                  '')
            signature = OpenSSL.crypto.sign(private,
                                           challenge,
                                           "sha256")
        except:
            _log.debug("signing poison find node failed")
        return { 'bucket' : mergedlist , 'signature' : signature }

    def poison_rpc_find_value(self, sender, nodeid, key, challenge, signature):
        value = self.storage[digest(str(self.sourceNode.id.encode("hex").upper()) + "cert")]
        if key == digest("APA") or \
           key == digest("KANIN") or \
           key == digest("KOALA"):
            logger(self.sourceNode,
                   "Attacking node with port {} sent back "
                   "forged value".format(self.router.node.port))
            value = "apelsin"
        try:
            private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                  self.priv_key,
                                                  '')
            signature = OpenSSL.crypto.sign(private,
                                           challenge,
                                           "sha256")
        except:
            _log.debug("signing poison find value failed")
        return { 'value': value, 'signature': signature }

    # def eclipse_rpc_find_value(self, sender, nodeid, key, challenge, signature):
    #     source = Node(nodeid, sender[0], sender[1])
    #     if long(nodeid.encode('hex'), 16) != long(self.closest_neighbour[0][0].encode('hex'), 16):
    #         _log.debug("Attacking node with port {} received value request from non-eclipsed node".format(self.router.node.port))
    #         self.maybeTransferKeyValues(source)
    #         self.router.addContact(source)
    #         exists, value = self.storage.get(key, None)
    #         if not exists:
    #             return self.rpc_find_node(sender, nodeid, key, challenge, signature)
    #         else:
    #             return { 'value': value }
    #     else:
    #         _log.debug("Attacking node with port {} received value request from eclipsed node".format(self.router.node.port))
    #         return { 'value' : 'apelsin' }


class evilAppendServer(niceAppendServer):
    def __init__(self, ksize=20, alpha=3, id=None, storage=None):
        storage = storage or append_server.ForgetfulStorageFix()
        append_server.Server.__init__(self,
                                     ksize,
                                     alpha,
                                     id,
                                     storage=storage)
        self.protocol = evilKademliaProtocolAppend(self.node,
                                                  self.storage,
                                                  ksize)

    def bootstrap(self, addrs):
        """
        Bootstrap the server by connecting to other known nodes in the network.
        Args:
            addrs: A `list` of (ip, port) `tuple` pairs.  Note that only IP addresses
                   are acceptable - hostnames will cause an error.
        """
        # if the transport hasn't been initialized yet, wait a second
        # _log.debug("bootstrap"
        if self.protocol.transport is None:
            return append_server.task.deferLater(service_discovery_ssdp.reactor,
                                                1,
                                                self.bootstrap,
                                                addrs)

        def initTable(results, challenge, id):
            nodes = []
            for addr, result in results.items():
                ip = addr[0]
                port = addr[1]
                if result[0]:
                    resultId = result[1]['id']
                    resultIdHex = resultId.encode('hex').upper()
                    resultSign = result[1]['signature']
                    data = self.protocol.certificateExists(resultIdHex)
                    if not data:
                        identifier = "{}cert".format(resultIdHex)
                        self.protocol.callCertFindValue(Node(resultId,
                                                            ip,
                                                            port),
                                                       Node(identifier))
                    else:
                        cert_stored = self.protocol.searchForCertificate(resultIdHex)
                        try:
                            OpenSSL.crypto.verify(cert_stored,
                                                 resultSign,
                                                 challenge,
                                                 "sha256")
                        except:
                            traceback.print_exc()
                        nodes.append(Node(resultId, ip, port))
            spider = NodeSpiderCrawl(self.protocol,
                                    self.node,
                                    nodes,
                                    self.ksize,
                                    self.alpha)
            return spider.find()

        ds = {}
        challenge = generate_challenge()
        id = None
        if addrs:
            data = addrs[0]
            addr = (data[0], data[1])
            try:
                cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                      data[2])
                fingerprint = cert.digest("sha256")
                id = fingerprint.replace(":", "")[-40:]
                private = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
                                                      self.protocol.priv_key,
                                                      '')
                signature = OpenSSL.crypto.sign(private,
                                                "{}{}".format(id, challenge),
                                                "sha256")
                ds[addr] = self.protocol.ping(addr,
                                             self.node.id,
                                             challenge,
                                             signature,
                                             self.protocol.getOwnCert())
            except:
                logger(self.protocol.sourceNode, "Certificate creation failed")
            self.protocol.storeCert(data[2], id)
            node = Node(id.decode("hex"), data[0], data[1])
            if self.protocol.router.isNewNode(node):
                return deferredDict(ds).addCallback(initTable, challenge, id)
        return deferredDict(ds)

def drawNetworkState(name, servers, amount_of_servers):
    """Save image describinh network of `servers` as `name`."""
    if pydot is None:
        return
    graph = pydot.Dot(graph_type='digraph',
                     nodesep=0,
                     ranksep=0,
                     rankdir="BT")
    for servno in range(0, amount_of_servers):
        rndnode = Node(hashlib.sha1(str(random.getrandbits(255))).digest())
        findNeighbors = servers[servno].dht_server.kserver.protocol.router.findNeighbors
        neighbors = map(tuple, findNeighbors(rndnode, k=50))
        for neighbor in neighbors:
            printPort = servers[servno].dht_server.port.getHost().port
            edge = pydot.Edge(str(printPort),
                             str(neighbor[2]),
                             label=str(neighbor[0].encode('hex')[-4:]))
            graph.add_edge(edge)
    graph.write_png(name)

def logger(node, message, level=None):
    _log.debug("{}:{}:{} - {}".format(node.id.encode("hex").upper(),
                                     node.ip,
                                     node.port,
                                     message))
    # print("{}:{}:{} - {}".format(node.id.encode("hex").upper(), node.ip, node.port, message))
