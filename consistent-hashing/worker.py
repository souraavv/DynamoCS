import os 
import sys 
import time 
import json
import math
import rpyc 
import socket 
import pickle
import random
import threading
import redis 

from hashlib import md5
from pprint import pprint
from bisect import bisect 
from json import JSONEncoder
from datetime import datetime 
from rpyc.utils.server import ThreadedServer
from typing import List, Set, Dict, Tuple, Callable, Iterator, Union, Optional, Any, Counter
 
'''
gossip_data:
    “end_of_range”: {ip_x, v_id, version_number, load, start_of_range}
    
'''

class VectorClock:
    def __init__(self, ip:str, port:int, version_number:int, load:float, start_of_range:int):
        self.ip:str = ip
        self.version_number:int = version_number
        self.load:float = load 
        self.port:int = port
        self.start_of_range:str = start_of_range
    
    #* From vector clock to dict.
    def to_dict(self) -> Dict:
        return {
            "ip": self.ip,
            "version_number": self.version_number,
            "load": self.load,
            "port": self.port,
            "start_of_range": self.start_of_range 
        }
    #* From dict to vector clock


class Worker(rpyc.Service):
    def __init__(self, port:int, redis_port:int) -> None:
        '''
        Constants (or configurable)
        '''
        self.FAILURE:int = -1  
        self.SUCCESS:int = 0  
        self.IGNORE:int = 1
        self.EXPIRE:int = 3
        self.INVALID_RESOURCE = 4
        self.GOSSIP_INTERVAL:int = 5
        self.PING_DOWN_NODE_INTERVAL:int = 30
        self.REPLICATED_TIMEOUT:int = 20
        self.REPLICA_RETRY = 2
        self.READ:int = 3 # Take it as config from client
        self.WRITE:int = 2 # Take it as config from client
        self.REDIS_WRITE_RETRY:int = 3 # to retry on watch error
        self.REDIS_PORT:int = redis_port 
        self.N = self.READ + self.WRITE + 1 # set it properly
        self.hashmap = f'hash-map-{port}'
        self.FORMAT = "%Y-%m-%d %H:%M:%S"
        '''
        Locks
        '''
        self.lock_routing_table = threading.Lock() # Active nodes
        self.lock_down_routing_table = threading.Lock() # Down nodes
        '''
        Meta data a worker need to have
        '''
        self.ip:str = 'localhost'
        self.port:int = port # port at which this worker node will server
        self.start_of_range:str = "-1" 
        self.end_of_range: str = "-1"
        self.hash_ring_url = ('10.194.58.46', 3000) # hash ring 
        self.routing_table = dict() #* Will store the routing table of active nodes
        self.down_routing_table = dict() #* Will store all those entry which are down now
        self.hash_function = (lambda key: int(md5(str(key).encode("utf-8")).hexdigest(), 16)) # same hash function is used in hashring
        self.requests_log = dict() # Used by background thread which will keep sending the data to these nodes (to satisfy replica property)
        self.get_requests_log = dict() # response_id -> (fresh_value, fresh_timestamp)
        '''
        Redis instance
        '''
        self.rds = redis.Redis(host='localhost', port=self.REDIS_PORT, db=0, decode_responses=True)

        '''
        Setting up daemon threads
        '''
        gossip_thread = threading.Thread(target=self.start_gossip, args = (), daemon=True)
        ping_down_node_thread = threading.Thread(target=self.thread_ping_down_node, args = (), daemon=True)
        replicate_thread = threading.Thread(target=self.replicate, args = (), daemon=True)

        gossip_thread.start()
        ping_down_node_thread.start()
        replicate_thread.start()
 


    def deserialize(self, routing_table):
        deserialize_routing_table = {}
        for hash, vc in routing_table.items():
            deserialize_routing_table[hash] = VectorClock(ip=vc['ip'],port=vc['port'], 
                                                          version_number=vc["version_number"], 
                                                          load=vc["load"], start_of_range=vc["start_of_range"])
        return deserialize_routing_table

    def serialize(self, routing_table):
        serialized_routing_table = {}
        for hash, vc in routing_table.items():
            serialized_routing_table[hash] = vc.to_dict()
        return serialized_routing_table

    # This function will fetch the keys from next 'x' nodes
    def fetch_keys(self):
        #TODO: Reconsillation
        pass 

    def exposed_init_table(self, routing_info):
        print ("New beginning")
        new_added = routing_info['new_added']
        self.routing_table[str(new_added['end_of_range'])] = VectorClock(ip=new_added['ip'],
        port=new_added['port'], version_number=new_added["version_number"],
        load=new_added["load"], start_of_range=new_added["start_of_range"]) 
        self.start_of_range = new_added['start_of_range']
        self.end_of_range = new_added['end_of_range']
        self.fetch_keys() 
        # self.print_routing_table()

    def exposed_update_table(self, routing_info:Dict[Any, Any]):
        ''' Update your starting point '''
        self.lock_routing_table.acquire()
        self.routing_table[str(self.end_of_range)].start_of_range = routing_info['new_start']
        self.routing_table[str(self.end_of_range)].version_number += 1
        ''' Add the fresh entry to the table '''
        new_added = routing_info['new_added']
        self.routing_table[str(new_added['end_of_range'])] = VectorClock(ip=new_added['ip'],
        port = new_added['port'], version_number = new_added["version_number"],
        load = new_added["load"], start_of_range = new_added["start_of_range"]) 
        self.lock_routing_table.release()
        # self.print_routing_table()
    
    '''
    This will be background thread which will keep running at some interval
    '''
    def ping(self, ip, port, timeout = 2):
        print (f"pinging.... {ip}:{port}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM) #presumably 
        sock.settimeout(timeout)
        try:
            sock.connect((ip, port))
        except:
            print ("cant still ping :(")
            return False
        else:
            print ("Can ping :)")
            sock.close()
            return True

    def ping_thread(self, to_ping):
        # print (f"need to ping: by:{self.port}: nodes = ",  to_ping)
        nodes = set(self.routing_table.keys())
        down_nodes = set(self.down_routing_table.keys())    
        for node in to_ping:  
            if node in down_nodes:
                vc = self.down_routing_table[str(node)]
                ip, port = vc.ip, int(vc.port)
                response = self.ping(ip, port)
                if response == True:
                    print ("Removing from down: ", node)
                    self.lock_down_routing_table.acquire()
                    del self.down_routing_table[str(node)]
                    self.lock_down_routing_table.release()
                    print ("Adding to active: ", node)
                    self.lock_routing_table.acquire()
                    # vc.version_number += 1
                    self.routing_table[node] = vc 
                    self.lock_routing_table.release()

            elif node in nodes: 
                vc = self.routing_table[str(node)]
                ip, port = vc.ip, int(vc.port)
                response = self.ping(ip, port)  
                if response == False:
                    print ("Removing from active: ", node)
                    self.lock_routing_table.acquire()
                    del self.routing_table[str(node)]
                    self.lock_routing_table.release()
                    print ("Adding to active: ", node)
                    self.lock_down_routing_table.acquire()
                    # vc.version_number += 1
                    self.down_routing_table[str(node)] = vc
                    self.lock_down_routing_table.release()

    '''
    This is a thread which will try to ping self down nodes
    '''
    def thread_ping_down_node(self):
        while True:
            if (len(self.down_routing_table)):
                print ("Pinging down nodes...")
                self.ping_thread(list(self.down_routing_table.keys()))
            time.sleep(self.PING_DOWN_NODE_INTERVAL)
            
    '''
    Start the gossip
    '''
    def start_gossip(self):
        while True: 
            time.sleep(self.GOSSIP_INTERVAL) # Take some rest before gossip
            self.print_routing_table()
            if (len(self.routing_table) or len(self.down_routing_table)):
                nodes = list(self.routing_table.keys())
                nodes.sort() # Get the nodes
                down_nodes = set(self.down_routing_table.keys()) # Get the down keys
                n:int = len(self.routing_table)
                idx:int = random.randint(0, n - 1)
                talk_to = nodes[idx]
                node:VectorClock = self.routing_table[talk_to] # pick a random node to talk
                url = (node.ip, node.port)
                if (node.ip == self.ip) and (node.port == self.port): # Can't gossip with self, need a friend
                    continue
                
                ask_guest_to_ping = list()
                try:
                    conn = rpyc.connect(*url) 
                    conn._config['sync_request_timeout'] = None 
                    '''
                    We are using 2-way communication to, in first go we send
                    both our active and down routing table and then in response
                    we are getting what entry we need to udpate
                    '''        
                    ''' Dump both active routing table and down routing table '''
                    routing_table, down_routing_table = pickle.dumps(self.serialize(self.routing_table)), pickle.dumps(self.serialize(self.down_routing_table))
                    ''' After doing chit chat get some gift '''
                    gift_routing_table, gift_down_routing_table, ask_guest_to_ping = conn.root.do_chit_chat(routing_table, down_routing_table)
                    ''' Unwrap the gift '''
                    gift_routing_table = self.deserialize(pickle.loads(gift_routing_table))
                    gift_down_routing_table = self.deserialize(pickle.loads(gift_down_routing_table))
                    ''' node here represent the hash(or end_of_key)
                    update the outdated entry in both routing and down routing table 
                    update your routing table '''
                    for node, vc in gift_routing_table.items():
                        if node not in self.routing_table.keys():
                            self.routing_table[node] = vc 
                    for node, vc in gift_down_routing_table.items():
                        if node not in self.down_routing_table.keys():
                            self.down_routing_table[node] = vc 
                    #* ping the nodes.
                except Exception as e: 
                    print ("Some thing bad happen while chit chat..", e)
                    ask_guest_to_ping.append(nodes[idx])

                ''' To all those whom I was not able to connect during chit-chat'''
                if len(ask_guest_to_ping): 
                    ping_thread = threading.Thread(target=self.ping_thread, args = (ask_guest_to_ping, ))
                    ping_thread.start()

    '''
    '''
    def exposed_do_chit_chat(self, routing_table, down_routing_table) -> Any:
        ''' Unwrap the gift '''
        routing_table = self.deserialize(pickle.loads(routing_table))
        down_routing_table = self.deserialize(pickle.loads(down_routing_table))
        ''' Prepare the gift '''
        gift_routing_table = {}
        gift_down_routing_table = {}

        guest_active_nodes = set(list(routing_table.keys()))
        guest_down_nodes = set(list(down_routing_table.keys()))

        self_active_nodes = set(list(self.routing_table.keys()))
        self_down_nodes = set(list(self.down_routing_table.keys()))

        '''
        If guest have some down keys which are active in mine, I need to tell guest
        to try to ping over those
        '''
        ask_guest_to_ping = self_active_nodes & guest_down_nodes
        no_need_to_ask_guest = set()
        need_to_ping = guest_active_nodes & self_down_nodes
        no_need_to_ping = set()
        '''
        If guest have some active keys which are in my down_keys, 
        I need to ping those
        '''
        for node in ask_guest_to_ping:
            if node in self.routing_table.keys():
                version_number = self.routing_table[node].vesion_number
                if (version_number < down_routing_table[node].version_number):
                    need_to_ping.add(node)
                    no_need_to_ask_guest.add(node) 
                 
        ask_guest_to_ping = ask_guest_to_ping - no_need_to_ask_guest
        
        for node in need_to_ping:
            version_number = self.down_routing_table[node].version_number
            #* If I have down record later point in time, then the node which 
            #* asked me to ping, then I will ask that node to ping
            if (version_number > routing_table[node].version_number):
                no_need_to_ping.add(node) # If my version number at down is more updated then the one who is asking me to udpate
                ask_guest_to_ping.add(node)

        need_to_ping = need_to_ping - no_need_to_ping
        
        ping_thread = threading.Thread(target=self.ping_thread, args = (need_to_ping, ))
        ping_thread.start()
        '''
        If entry is active in both, and I'm the most updated I will send this
        as gift to the guest in routing table
        '''
        active_in_both = self_active_nodes & guest_active_nodes
        for node in active_in_both:
            if routing_table[node].version_number < self.routing_table[node].version_number:
                gift_routing_table[node] = self.routing_table[node]
            elif routing_table[node].version_number > self.routing_table[node].version_number:
                self.lock_routing_table.acquire()
                self.routing_table[node] = routing_table[node]
                self.lock_routing_table.release()
        '''
        If entry is down in both then, update your if you have stale, if you
        have much updated then ask guest to udpate
        '''
        down_in_both = self_down_nodes & guest_down_nodes
        for node in down_in_both:
            if down_routing_table[node].version_number < self.down_routing_table[node].version_number:
                gift_down_routing_table[node] = self.down_routing_table[node]
            elif down_routing_table[node].version_number > self.down_routing_table[node].version_number:
                self.lock_down_routing_table.acquire()
                self.down_routing_table[node] = down_routing_table[node]    
                self.lock_down_routing_table.release()    
        '''
        A fresh entry which I haven't seen before
        '''
        surprise_gift_from_guest = guest_active_nodes - self_active_nodes
        for node in surprise_gift_from_guest:
            if node not in self_down_nodes:
                self.lock_routing_table.acquire()
                self.routing_table[node] = routing_table[node]
                self.lock_routing_table.release()

        surprise_gift_from_guest = guest_down_nodes - self_down_nodes
        for node in surprise_gift_from_guest:
            if node not in self_active_nodes:
                self.lock_down_routing_table.acquire()
                self.down_routing_table[node] = down_routing_table[node]
                self.lock_down_routing_table.release()
        '''
        A fresh entry which guest haven't seen before 
        '''
        surprise_gift_to_guest = self_active_nodes - guest_active_nodes
        for node in surprise_gift_to_guest:
            if node not in guest_down_nodes: 
                gift_routing_table[node] = self.routing_table[node]

        surprise_gift_to_guest = self_down_nodes - guest_down_nodes
        for node in surprise_gift_to_guest:
            if node not in guest_active_nodes:
                gift_down_routing_table[node] = self.down_routing_table[node]
        
            
        ''' wrap the gift'''
        gift_routing_table = pickle.dumps(self.serialize(gift_routing_table))
        gift_down_routing_table = pickle.dumps(self.serialize(gift_down_routing_table))
        
        print ("done chit-chat")
        return (gift_routing_table, gift_down_routing_table, list(ask_guest_to_ping))

    '''
    This function will be called by client for getting the appropriate routing
    tables.
    '''
    def exposed_fetch_routing_info(self, key:str, need_serialized=True):
        print ("SOME ONE NEED ROUTING TABLES...")
        self_active_nodes = list(self.routing_table.keys())
        self_active_nodes.sort()
        key_hash = str(self.hash_function(key))
        idx = bisect(self_active_nodes, key_hash)
        idx = 0 if idx == len(self_active_nodes) else idx
        controller_node = self_active_nodes[idx]
        replica_nodes = {}
        n = len(self_active_nodes)
        # since it is a ring, we need to do %
        for pos in range(0, min(len(self_active_nodes), self.N)):
            node_hash = self_active_nodes[(idx + pos) % n]
            replica_nodes[node_hash] = self.routing_table[node_hash] 

        if need_serialized:
            replica_nodes = pickle.dumps(self.serialize(replica_nodes))
        print ("SENDING ROUTING TABLE")
        return replica_nodes, controller_node

    def print_routing_table(self):
        print ("--" *10, "Routing table of ", self.port, "--"* 10)
        print("--"*10, "Active Routing table", "--" * 10)
        for node, vc in self.routing_table.items():
            print (f"[{int(vc.start_of_range) % 1000}, {int(node) % 1000}]| url = ({vc.ip}, {vc.port}), version = {vc.version_number} | Load = {vc.load}")
        print("--"*10, "Down Routing Table", "--" * 10)
        for node, vc in self.down_routing_table.items():
            print (f"[{int(vc.start_of_range) % 1000} , {int(node) % 1000}]| url = ({vc.ip}, {vc.port}), version = {vc.version_number} | Load = {vc.load}")   
        print("--"*25)

    def exposed_replicated_put(self, key, value, request_id, timestamp):
        print (f"Replica are called on {self.ip}, {self.port} for put..{key} = {value}")
        node = self.end_of_range
        # self.rds.set_response_callback('GET', float)
        print ("TYPE OF SELF.RDS.GET(key): ", type(self.rds.get(key)))

        if (type(self.rds.get(key) == None)):
            print ("BEKAR CODE First time run-----------------------")
            stored_timestamp = timestamp
        else:
            stored_timestamp = float(self.rds.get(key))
        print ("Type of stored_timestamp: ", type(stored_timestamp), stored_timestamp)
        print ("Type of timestamP: ", type(timestamp), timestamp) 
        
        if stored_timestamp > timestamp:
            return {"status": self.IGNORE, "msg": "I am more updated than you!"} 
        retry_count:int = self.REDIS_WRITE_RETRY
        with self.rds.pipeline() as pipe:
            pipe.watch(self.hashmap)
            while True:
                if retry_count == 0:
                    print ("Failure in replication..:(")
                    return {"status": self.FAILURE, "msg": "Cannot update the redis of replica"}
                try:
                    pipe.multi()
                    pipe.hset(self.hashmap, key, value)
                    pipe.set(key, timestamp)
                    pipe.execute()
                    break 
                except redis.WatchError:
                    print ("Some Redis watch error...")
                    retry_count -= 1
                    continue
        print ("Replcation done..")
        return {"status": self.SUCCESS, "request_id": request_id, "node": node, "msg": "Success"}
    
    '''
    This is a thread which will use the state of self.request_logs
    and based on that it will try to replicated (key, value) on self.N replicas
    Each key, value, node has a retry_count which is set to self.REPLICA_RETRY
    after that we stop retrying
    To save lot of rpc calls we are using piggy backing and thus saving lot 
    of unnecessary calls
    '''

    def wait_for_responses(self, responses, required):
        print ("Waiting for responses...")
        count_success_responses = 0
        count_error_responses = 0
        while True:
            try:
                print ("waiting...")
                print (f'success: {count_success_responses}, error = {count_error_responses}')
                if count_success_responses >= required:
                    print ("Done with waiting for reponses: success")
                    return {"status": self.SUCCESS} 
                if count_error_responses > self.N - required:
                    print ("Done with waiting for reponses: failure")
                    return {"status": self.FAILURE}
                for response in responses:
                    print ("response: = ", response, response.ready)
                    if response.ready:
                        res = response.value
                        print ("Ok so reponse is: ", res)
                        if res['status'] == self.SUCCESS:
                            count_success_responses += 1 
                        else:
                            count_error_responses += 1
                    else:
                        print ("not ready!", response.ready)
            except Exception as e:
                print ("Something bad happen ", e)
                # break

        print ("Done with waiting for reponses")
    def replicate(self):
        print ("Replicate called")
        piggy_backing = dict() 
        stop_retrying = [] # store all those nodes whose retry count reached zero
        for request_id, replicas in self.requests_log.items():
            key, value, timestamp =  self.requests_log[request_id]['info']
            for node, status in replicas.items():
                if node == 'info':
                    continue
                if status.retry_count <= 0:
                    stop_retrying.append(request_id)
                    continue 
                if status.status == self.FAILURE:
                    self[request_id][node].retry_count -= 1
                    if node not in piggy_backing.keys():
                        piggy_backing[node] = []
                    piggy_backing[node].append({
                        "key": key,
                        "value": value, 
                        "timestamp": timestamp,
                        "request_id": request_id
                    })
            #* Delete those for which we will not retry now.
            for node in stop_retrying:
                del self[request_id][node]

        '''
        Used to approve success and ignored request so that they 
        can be removed from the self.requests_log
        '''
        def callback(res):
            print (f"Callback called for replicating...{res}")

            success_requests, ignored_requests, node = res 
            
            for request_id in success_requests:
                self.requests_log[request_id]["replicated_on"] += 1
                del self.requests_log[request_id][node]
            for request_id in ignored_requests:
                del self.requests_log[request_id][node]

        '''
        Do a rpyc call to send the piggyback
        '''
        print (f"Piggy backing lot of request: {piggy_backing}")
        responses = []
        for node, requests in piggy_backing.items():
            ip, port = self.routing_table[node].ip, self.routing_table[node].port 
            conn = rpyc.connect(ip, port)
            async_func = rpyc.async_(conn.root.bulk_put)
            res = async_func(requests)
            res.add_callback(callback)
            res.set_expiry(self.EXPIRE)
            responses.append(res) 

        waiting = self.wait_for_responses(responses, len(responses))

    '''
    This is used by piggybacks so that put can be handled for mutiple
    key, values which are going to the same node
    '''
    def exposed_bulk_put(self, requests):
        print (f"Bulk of request arrived: {requests}")
        success_requests, ignored_requests = [], []
        for request in requests:
            result = self.exposed_replicas_put(key=request.key, value=request.value, request_id=request.request_id, timestamp=request.timestamp)
            if result.status == self.SUCCESS:
                success_requests.append(request.request_id)
            elif result.status == self.IGNORE:
                ignored_requests.append(request.request_id)
        return success_requests, ignored_requests, self.end_of_range

    '''
    Below get is made for {key: value} for password like services only

    This method is exposed to client for reads
    - Node will fetch the next N entry from the routing table
    and ask all of them the key: value and timestamp

    '''

    def exposed_get_key(self, key, request_id):
        print ('--' * 5, "Someone need get info", '--' * 5)
        #!FIXME: add exception hadling please...
        return {"timestamp": self.rds.get(key), 
        "value": self.rds.hget(self.hashmap, key), 
        "request_id": request_id,
        "node": self.end_of_range,
        "status": self.SUCCESS}

    def make_request_id(self, key):
        timestamp = time.time()
        now = str(timestamp)
        return str(self.hash_function(now + str(key))), timestamp

    '''
    Called by client
    '''
    def exposed_get(self, key):
        print (f"GET CALLED FOR KEY = {key}")
        request_id, _ = self.make_request_id(key)
        start, end = self.start_of_range, self.end_of_range
        replica_nodes, controller_node = self.exposed_fetch_routing_info(key=key, need_serialized=False)
        timestamp = self.rds.get(key)
        key_hash = str(self.hash_function(key))
        count_responses, fresh_value, fresh_timestamp = 0, self.rds.hget(self.hashmap, key), self.rds.get(key)
        self.get_requests_log[request_id] = {"fresh_value": fresh_value, "fresh_timestamp": fresh_timestamp, "count_responses": count_responses}
        self.get_requests_log[request_id + '__NODE__']  = []

        if ((start > end and (key_hash >= start or key_hash <= end)) or (start <= key_hash and key_hash < end)):    
            print ("At the right node for READ!....")
            def callback(response):
                try:
                    print ("Callback for GET Called!")
                    res = response.value
                    print ("res = ", res)
                    if (res == None):
                        return
                    timestamp = float(res['timestamp'])
                    value = res['value']
                    request_id = res['request_id']
                    node = res['node']
                    print (f'timestamp = {timestamp}, value = {value}, request_id = {request_id}, node = {node}')
                    if (timestamp is None) or (value is None):
                        return
                    self.get_requests_log[request_id]['count_responses'] += 1 
                    fresh_timestamp = float(self.get_requests_log[request_id]['fresh_timestamp'])
                    print ("Fresh timestamp: ", fresh_timestamp)
                    if timestamp == fresh_timestamp:
                        self.get_requests_log[request_id + '__NODE__'].append(node)
                    elif timestamp > fresh_timestamp:
                        print ("update time")
                        self.get_requests_log[request_id]['fresh_value'] = value;
                        self.get_requests_log[request_id]['fresh_timestamp'] = timestamp
                        self.get_requests_log[request_id + '__NODE__'].append(node)
                except Exception as e:
                    print ("Something bad happen in exposed_get ", e)
            responses = []
            for node in replica_nodes:
                if node in self.routing_table.keys():
                    vc = self.routing_table[node]
                    conn = rpyc.connect(vc.ip, vc.port)
                    async_func = rpyc.async_(conn.root.get_key)
                    res = async_func(key, request_id)
                    res.add_callback(callback)
                    res.set_expiry(self.EXPIRE)
                    responses.append(res) 
            
            print (f' Responses : {responses}')
            print ("Waiting for get...")

            waiting = self.wait_for_responses(responses, self.READ)
            print ("Wait done..")
            if waiting['status'] == self.SUCCESS: 
                return {"status": self.SUCCESS, "value": {self.get_requests_log[request_id]['fresh_value']}}
            else: 
                return {"status": self.FAILURE, "msg": "Service unavailable! Retry again"}
            
        else:
            return {'status': self.INVALID_RESOURCE, 'replica_nodes': replica_nodes, 'controller_node': controller_node}

    def exposed_put(self, key, value):
        print (f"PUT REQUEST: key = {key}, value = {value}")
        request_id, timestamp = self.make_request_id(key) # generate a unique request id
        key_hash = str(self.hash_function(key)) # to locate the key in the ring
        start, end = self.start_of_range, self.end_of_range 
        replica_nodes, controller_node = self.exposed_fetch_routing_info(key=key, need_serialized=False)
        
        if ((start > end and (key_hash >= start or key_hash <= end)) or (start <= key_hash and key_hash < end)):    
            print ("OK, Correct node (controller) to put")
            print ("Writing to REDIS..")
            
            with self.rds.pipeline() as pipe:
                pipe.watch(self.hashmap)
                while True:
                    try:
                        pipe.multi()
                        pipe.hset(self.hashmap, key, value)
                        pipe.set(key, timestamp)
                        pipe.execute()
                        break 
                    except redis.WatchError as e:
                        print ("Watch error: ", e)

                        continue
            #* controller node is not always the first node.(May be I'm a programmer)
            print ("Done writing to redis")
            if len(self.routing_table) < self.WRITE:
                return {"status": self.FAILURE, "msg": "Not enough replicas to write, Please try later!"}

            '''
            Below is the logic to replicate on the replicas
            callback() is used by async call to write on replicas
            callback() in case receiving success will remove it remove request logs
            and if it recieve ignore (in case replica is more updated for that key)
            then it will simply add 
            '''
            ''' This is a callback function used by async thread. '''
            def callback(response):
                try:
                    print ("----Callback called for put!--", type(response))
                    res = response.value
                    print ("res: ", res)
                    request_id = res['request_id']
                    node = res['node']
                    self.requests_log[request_id]["replicated_on"] += 1
                    if (res['status'] == self.SUCCESS) or (res['status'] == self.IGNORE):
                        del self.requests_log[request_id][node]
                except Exception as e:
                    print ("Something bad happend in put: ", e)    
        
            ''' Add to the requests_logs, so that background thread can run '''
            SEND_RPC = 1

            print ("Request id: ", request_id)
            if request_id not in self.requests_log.keys():
                self.requests_log[request_id] = {"info": (), "replicated_on": 0}
            self.requests_log[request_id]["info"] = (key, value, timestamp)
            self.requests_log[request_id]["replicated_on"] = 0
            for node, vc in replica_nodes.items():
                if node != self.end_of_range:
                    # if request_id in self.requests_log.keys():
                    self.requests_log[request_id][node] = {"status": SEND_RPC, "retry_count": self.REPLICA_RETRY}
            '''
            Try to send the async rpyc request to the replica node 
            So that they can have key, value stored.
            '''
            responses = []
            for node, vc in replica_nodes.items():
                if node != self.end_of_range: #* End of range represent node hash
                    try:
                        conn = rpyc.connect(vc.ip, vc.port)
                        async_func = rpyc.async_(conn.root.replicated_put)
                        res = async_func(key, value, request_id, timestamp)
                        res.add_callback(callback)
                        res.set_expiry(self.EXPIRE)
                        responses.append(res)
                    except Exception as e:
                        print ("Some thing bad happen at put ", e)
                        pass 
            '''
                Now wait for the W to finish the writes and once they are done
                we are free to repsonse to the client for their write
                and our background thread will try to make it to write to N replicas
            '''
            # time.sleep(5) # this will be replced by wait
            print ("responses: ", responses)
            waiting = self.wait_for_responses(responses, self.WRITE) 
            if waiting['status'] == self.SUCCESS:
                return {"status": self.SUCCESS, "msg": f"Successfully wrote {key} = {value}", "version_number": -1} 
            else: 
                return {"status": self.FAILURE, "msg": "Service unavailable! Retry again"}

        else:
            #* Return the node which should contain this key, if I'm not the controller
            #* of that key any more/ or was never.
            return {'status': self.INVALID_RESOURCE, 'replica_nodes': replica_nodes, 'controller_node': controller_node}



if __name__ == '__main__':
    port = int(sys.argv[1])
    redis_port = int(6379)
    print (f"Listenting worker at {port}...")
    ThreadedServer(Worker(port, redis_port), hostname='0.0.0.0', port=port, protocol_config={'allow_public_attrs': True}).start()
    