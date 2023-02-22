import redis 
import uuid
import rpyc
import string
import sys
import time
from random import choice, randint
from collections import Counter

def get_random_string(length):
    letters = string.ascii_lowercase
    result_str = ''.join(choice(letters) for i in range(length))
    return result_str 

def test_hashring():
    url = ('localhost', 3000)
    count = int(sys.argv[1])
    print ("allocating nodes...")
    ret = rpyc.connect(*url).root.allocate_nodes(count)

    # val = rpyc.connect(*url).root.get_all_node_location()
    # print (f'All nodes: {val}')
    print (ret)
    if ret["status"] == -1:
        print(f"Reached maximum limit of resources : left {ret['output']}")
    else:
        for j in range(0, 100):
            key, value =  ''.join(choice(ascii_uppercase) for i in range(12)), ''.join(choice(ascii_lowercase) for i in range(4))
            rpyc.connect(*url).root.put(key, value)
            ret = rpyc.connect(*url).root.get(key)
            # print (ret)
        val = rpyc.connect(*url).root.get_all_node_location()
        print (f'All nodes: {val}')


def test_spawn_wokers(nodes):
    url = ('localhost', 3000)
    conn = rpyc.connect(*url)
    conn._config['sync_request_timeout'] = None 
    res = conn.root.allocate_nodes(nodes)
    print (res)
    
def test_put():
    url = ('localhost', 3000)
    conn = rpyc.connect

def test_client_put(key, value):
    url = ('localhost', 6001)
    conn = rpyc.connect(*url)
    conn._config['sync_request_timeout'] = None 
    print (f'PUT REQUEST: For {key} = {value}')
    res = conn.root.put(key, value)
    print (f'PUT RESPONSE: {res}')

def test_client_get(key):
    url = ('localhost', 6001)
    conn = rpyc.connect(*url)
    conn._config['sync_request_timeout'] = None 
    
    
    print (f'GET REQUEST : For {key}')
    res = conn.root.get(key)
    print (f'GET REPONSE for key {key} = {res}')


def test_workers():
    url = ('localhost', 3000)
    conn = rpyc.connect(*url).root
    res = conn.get()
    

which = int(sys.argv[1])
if which == 1:
    test_hashring() #DONE
if which == 2: 
    nodes = int(sys.argv[2])
    test_spawn_wokers(nodes)
if which == 3:
    for i in range(0, 50):
        s = get_random_string(25)
        test_client_put(s, randint(1, 10))

if which == 4:
    test_client_get('yepdt')
# test_workers()

