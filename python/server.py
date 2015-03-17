#! /usr/bin/env python

import SimpleHTTPServer
import SocketServer
import BaseHTTPServer
import cgi
import logging
import urllib
import sys, os
import time
import thread
import threading
import sys
from math import log, exp
from thread import start_new_thread
from exchanges import *
from utils import *

class ThreadingServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    pass


# pool configuration
_port = 2020
# daily interest rates
_interest = { 'poloniex' : { 'btc' : { 'rate' : 0.0025, 'target' : 200.0, 'fee' : 0.002 } },
              'ccedk' : { 'btc' : { 'rate' : 0.0025, 'target' : 200.0, 'fee' : 0.002 } },
              'bitcoincoid' : { 'btc' : { 'rate' : 0.0025, 'target' : 200.0, 'fee' : 0.0 } },
              'bter' : { 'btc' : { 'rate' : 0.0025, 'target' : 200.0, 'fee' : 0.002 } } }
_nuconfig = '%s/.nu/nu.conf'%os.getenv("HOME") # path to nu.conf
_tolerance = 0.0085 # price tolerance
_sampling = 20 # number of requests validated per minute
_autopayout = True # try to send payouts automatically
_minpayout = 0.03 # minimum balance to trigger payout
_grantaddress = "" # custodian grant address

try: os.makedirs('logs')
except: pass

dummylogger = logging.getLogger('null')
dummylogger.addHandler(logging.NullHandler())
dummylogger.propagate = False

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler('logs/%d.log' % time.time())
fh.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)

formatter = logging.Formatter(fmt = '%(asctime)s %(levelname)s: %(message)s', datefmt="%Y/%m/%d-%H:%M:%S")
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

_wrappers = { 'poloniex' : Poloniex(), 'ccedk' : CCEDK(), 'bitcoincoid' : BitcoinCoId(), 'bter' : BTER() }
_liquidity = []

keys = {}
pricefeed = PriceFeed(30, logger)
lock = threading.Lock()

class NuRPC():
  def __init__(self, config, address, logger = None):
    self.logger = logger if logger else logging.getLogger('null')
    self.address = address
    self.rpc = None
    try:
      import jsonrpc
    except ImportError:
      self.logger.warning('NuRPC: jsonrpc library could not be imported')
    else:
      # rpc connection
      self.JSONRPCException = jsonrpc.JSONRPCException
      opts = dict(tuple(line.strip().replace(' ','').split('=')) for line in open(config).readlines())
      if not 'rpcuser' in opts.keys() or not 'rpcpassword' in opts.keys():
        self.logger.error("NuRPC: RPC parameters could not be read")
      else:
        try:
          self.rpc = jsonrpc.ServiceProxy("http://%s:%s@127.0.0.1:%s"%(
            opts['rpcuser'],opts['rpcpassword'], 14002))
          self.txfee = self.rpc.getinfo()['paytxfee']
        except:
          self.logger.error("NuRPC: RPC connection could not be established")
          self.rpc = None

  def pay(self, txout):
    try:
      self.rpc.sendmany("", txout)
      self.logger.info("successfully sent payout: %s", txout)
      return True
    except AttributeError:
      self.logger.error('NuRPC: client not initialized')
    except self.JSONRPCException as e:
      self.logger.error('NuRPC: unable to send payout: %s', e.error['message'])
    except:
      self.logger.error("NuRPC: unable to send payout (exception caught): %s", sys.exc_info()[1])
    return False

  def liquidity(self, bid, ask):
    try:
      self.rpc.liquidityinfo('B', bid, ask, self.address)
      print response
      self.logger.info("successfully sent liquidity: buy: %.8f sell: %.8f", bid, ask)
      return True
    except AttributeError:
      self.logger.error('NuRPC: client not initialized')
    except self.JSONRPCException as e:
      self.logger.error('NuRPC: unable to send liquidity: %s', e.error['message'])
    except:
      self.logger.error("NuRPC: unable to send liquidity (exception caught): %s", sys.exc_info()[1])
    return False

class User(threading.Thread):
  def __init__(self, key, address, unit, exchange, pricefeed, sampling, tolerance, logger = None):
    threading.Thread.__init__(self)
    self.key = key
    self.active = False
    self.address = address
    self.balance = 0.0
    self.pricefeed = pricefeed
    self.unit = unit
    self.exchange = exchange
    self.tolerance = tolerance
    self.sampling = sampling
    self.last_error = ""
    self.liquidity = { 'ask' : [[]] * sampling, 'bid' : [[]] * sampling }
    self.lock = threading.Lock()
    self.trigger = threading.Lock()
    self.trigger.acquire()
    self.response = ['m'] * sampling
    self.logger = logger if logger else logging.getLogger('null')
    self.requests = []
    self.daemon = True

  def set(self, request, sign):
    self.lock.acquire()
    if len(self.requests) < 10: # don't accept more requests to avoid simple spamming
      self.requests.append(({ p : v[0] for p,v in request.items() }, sign))
    self.active = True
    self.lock.release()

  def run(self):
    while True:
      self.trigger.acquire()
      self.lock.acquire()
      if self.active:
        del self.response[0]
        if self.requests:
          for rid, request in enumerate(self.requests):
            try:
              orders = self.exchange.validate_request(self.key, self.unit, *request)
            except:
              orders = { 'error' : 'exception caught: %s' % sys.exc_info()[1]}
            if not 'error' in orders:
              self.last_error = ""
              valid = { 'bid': [], 'ask' : [] }
              price = self.pricefeed.price(self.unit)
              for order in orders:
                deviation = 1.0 - min(order['price'], price) / max(order['price'], price)
                if deviation <= self.tolerance:
                  valid[order['type']].append((order['id'], order['amount']))
                else:
                  self.last_error = 'unable to validate request: order of deviates too much from current price'
              for side in [ 'bid', 'ask' ]:
                del self.liquidity[side][0]
                self.liquidity[side].append(valid[side])
              if self.last_error != "" and len(valid['bid'] + valid['ask']) == 0:
                self.response.append('r')
                self.logger.warning("unable to validate request %d/%d for user %s at exchange %s on unit %s: orders of deviate too much from current price" % (rid + 1, len(self.requests), self. self.key, repr(self.exchange), self.unit))
              else:
                self.response.append('a')
                break
            else:
              self.response.append('r')
              self.last_error = "unable to validate request: " + orders['error']
              self.logger.warning("unable to validate request %d/%d  for user %s at exchange %s on unit %s: %s" % (rid + 1, len(self.requests), self.key, repr(self.exchange), self.unit, orders['error']))
              for side in [ 'bid', 'ask' ]:
                del self.liquidity[side][0]
                self.liquidity[side].append([])
        else:
          self.response.append('m')
          self.last_error = "no request received"
          #logger.debug("no request received for user %s at exchange %s on unit %s" % (self.key, repr(self.exchange), self.unit))
          for side in [ 'bid', 'ask' ]:
            self.liquidity[side] = self.liquidity[side][1:] + [[]]
        self.requests = []
      self.lock.release()

  def validate(self):
    try: self.trigger.release()
    except thread.error: pass # user did not finish last request in time

  def finish(self):
    self.lock.acquire()
    self.lock.release()

def response(errcode = 0, message = 'success'):
  return { 'code' : errcode, 'message' : message }

def register(params):
  ret = response()
  if set(params.keys()) == set(['address', 'key', 'name']):
    user = params['key'][0]
    name = params['name'][0]
    if name in _wrappers:
      if not user in keys:
        lock.acquire()
        keys[user] = {}
        for unit in _interest[name]:
          keys[user][unit] = User(user, params['address'][0], unit, _wrappers[name], pricefeed, _sampling, _tolerance, logger)
          keys[user][unit].start()
        lock.release()
        logger.info("new user %s on %s: %s" % (user, name, params['address'][0]))
      elif keys[user].values()[0].address != params['address'][0]:
        ret = response(9, "user already exists with different address: %s" % user)
    else:
      ret = response(8, "unknown exchange requested: %s" % name)
  else:
    ret = response(7, "invalid registration data received: %s" % str(params))
  return ret

def liquidity(params):
  ret = response()
  if set(params.keys() + ['user', 'sign', 'unit']) == set(params.keys()):
    user = params.pop('user')[0]
    sign = params.pop('sign')[0]
    unit = params.pop('unit')[0]
    if user in keys:
      if unit in keys[user]:
        keys[user][unit].set(params, sign)
      else:
        ret = response(12, "unit for user %s not found: %s" % (user, unit))
    else:
        ret = response(11, "user not found: %s" % user)
  else:
    ret = response(10, "invalid liquidity data received: %s" % str(params))
  return ret

def poolstats():
  return { 'liquidity' : ([ (0,0) ] + _liquidity)[-1], 'sampling' : _sampling, 'users' : len(keys.keys()) }

def userstats(user):
  res = { 'balance' : 0.0, 'efficiency' : 0.0, 'rejects': 0, 'missing' : 0 }
  res['units'] = {}
  for unit in keys[user]:
    if keys[user][unit].active:
      bid = [[]] + [ x for x in keys[user][unit].liquidity['bid'] if x ]
      ask = [[]] + [ x for x in keys[user][unit].liquidity['ask'] if x ]
      missing = keys[user][unit].response.count('m')
      rejects = keys[user][unit].response.count('r')
      res['balance'] += keys[user][unit].balance
      res['missing'] += missing
      res['rejects'] += rejects
      res['units'][unit] = { 'bid' : bid[-1], 'ask' : ask[-1],
                             'rejects' : rejects,
                             'missing' : missing,
                             'last_error' :  keys[user][unit].last_error }
  if len(res['units']) > 0:
    res['efficiency'] = 1.0 - (res['rejects'] + res['missing']) / float(_sampling * len(res['units']))
  return res

def calculate_interest(balance, amount, interest):
  return max(min(amount, interest['target'] - balance) * interest['rate'], 0.0)
  #try: # this is not possible with python floating arithmetic
  #  return interest['rate'] * (amount - (log(exp(interest['target']) + exp(balance + amount)) - log(exp(interest['target']) + exp(balance))))
  #except OverflowError:
  #  logger.error("overflow error in interest calculation, balance: %.8f amount: %.8f", balance, amount)
  #  return 0.00001

def credit():
  for name in _interest:
    for unit in _interest[name]:
      users = [ k for k in keys if unit in keys[k] and repr(keys[k][unit].exchange) == name ]
      for side in [ 'bid', 'ask' ]:
        for sample in xrange(_sampling):
          orders = []
          for user in users:
            orders += [ (user, order) for order in keys[user][unit].liquidity[side][sample] ]
          orders.sort(key = lambda x: x[1][0])
          balance = 0.0
          previd = -1
          for user, order in orders:
            if order[0] != previd:
              previd = order[0]
              payout = calculate_interest(balance, order[1], _interest[name][unit]) / (_sampling * 60 * 24)
              keys[user][unit].balance += payout
              logger.info("credit [%d/%d] %.8f nbt to %s for %.8f %s liquidity on %s for %s at balance %.8f", sample + 1, _sampling, payout, user, order[1], side, name, unit, balance)
              balance += order[1]
            else:
              logger.warning("duplicate order id detected for user %s on exchange %s: %d", user, name, previd)

def pay(nud):
  txout = {}
  lock.acquire()
  for user in keys:
    for unit in keys[user]:
      if not keys[user][unit].address in txout:
        txout[keys[user][unit].address] = 0.0
      txout[keys[user][unit].address] += keys[user][unit].balance
  lock.release()
  txout = {k : v - nud.txfee for k,v in txout.items() if v - nud.txfee > _minpayout}
  if txout:
    payed = False
    if _autopayout:
      payed = nud.pay(txout)
    try:
      filename = 'logs/%d.credit' % time.time()
      out = open(filename, 'w')
      out.write(json.dumps(txout))
      out.close()
      if not payed:
        logger.info("successfully stored payout to %s: %s", filename, txout)
      lock.acquire()
      for user in keys:
        for unit in keys[user]:
          if keys[user][unit].address in txout:
            keys[user][unit].balance = 0.0
      lock.release()
    except: logger.error("failed to store payout to %s: %s", filename, txout)
  else:
    logger.warning("not processing payouts because no valid balances were detected.")

def submit(nud):
  curliquidity = [0,0]
  lock.acquire()
  for user in keys:
    for unit in keys[user]:
      for s in xrange(_sampling):
        curliquidity[0] += sum([ order[1] for order in keys[user][unit].liquidity['bid'][-(s+1)] ])
        curliquidity[1] += sum([ order[1] for order in keys[user][unit].liquidity['ask'][-(s+1)] ])
  lock.release()
  curliquidity = [ curliquidity[0] / float(_sampling), curliquidity[1] / float(_sampling) ]
  _liquidity.append(curliquidity)
  nud.liquidity(curliquidity[0], curliquidity[1])

class RequestHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
  def do_POST(self):
    if self.path in ['register', 'liquidity']:
      ctype, pdict = cgi.parse_header(self.headers.getheader('content-type'))
      if ctype == 'application/x-www-form-urlencoded':
        length = int(self.headers.getheader('content-length'))
        params = cgi.parse_qs(self.rfile.read(length), keep_blank_values = 1)
        if self.path == 'liquidity':
          ret = liquidity(params)
        elif self.path == 'register':
          ret = register(params)
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      self.wfile.write(json.dumps(ret))
      self.end_headers()

  def do_GET(self):
    method = self.path[1:]
    if method in [ 'status', 'exchanges' ]:
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      if method == 'status':
        self.wfile.write(json.dumps(poolstats()))
      elif method == 'exchanges':
        self.wfile.write(json.dumps(_interest))
      self.end_headers()
    elif method in keys:
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.wfile.write("\n")
      self.wfile.write(json.dumps(userstats(method)))
      self.end_headers()
    elif '/' in method:
      root = method.split('/')[0]
      method = method.split('/')[1]
      if root == 'price':
        price = { 'price' : pricefeed.price(method) }
        if price['price']:
          self.send_response(200)
          self.send_header('Content-Type', 'application/json')
          self.wfile.write("\n")
          self.wfile.write(json.dumps(price))
          self.end_headers()
        else:
          self.send_response(404)
      else:
        self.send_response(404)
    else:
      self.send_response(404)

  def log_message(self, format, *args): pass

nud = NuRPC(_nuconfig, _grantaddress, logger)
if not nud.rpc: logger.critical('Connection to Nu daemon could not be established, liquidity will NOT be sent!')
httpd = ThreadingServer(("", _port), RequestHandler)
sa = httpd.socket.getsockname()
logger.debug("Serving on %s port %d", sa[0], sa[1])
start_new_thread(httpd.serve_forever, ())

lastcredit = time.time()
lastpayout = time.time()
lastsubmit = time.time()

while True:
  try:
    curtime = time.time()

    # wait for validation round to end:
    lock.acquire()
    for user in keys:
      for unit in keys[user]:
        keys[user][unit].finish()
    lock.release()

    # send liquidity
    if curtime - lastsubmit >= 60:
      submit(nud)
      lastsubmit = curtime

    # credit requests
    if curtime - lastcredit >= 60:
      credit()
      lastcredit = curtime

    # make payout
    if curtime - lastpayout >= 3600: #43200:
      pay(nud)
      lastpayout = curtime

    # start new validation round
    lock.acquire()
    for user in keys:
      for unit in keys[user]:
        keys[user][unit].validate()
    lock.release()

    time.sleep(max(float(60 / _sampling) - time.time() + curtime, 0))
  except Exception as e:
    logger.error('exception caught: %s', sys.exc_info()[1])
    break

httpd.socket.close()
