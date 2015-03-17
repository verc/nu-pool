import sys
import json
import hmac
import time
import urllib
import urllib2
import hashlib
import httplib
import threading
import datetime

class Exchange(object):
  def __init__(self, domain):
    self._domain = domain
    self._shift = 1

  def adjust(self, error):
    if not 'exception caught:' in error:
      self._shift = ((self._shift + 7) % 200) - 100 # -92 15 -78 29 -64 43 -50 57 ...

class Poloniex(Exchange):
  def __init__(self):
    super(Poloniex, self).__init__('poloniex.com/tradingApi')
    self._shift = 1
    self._nonce = 0

  def __repr__(self): return "poloniex"

  def adjust(self, error):
    if "Nonce must be greater than" in error: # (TODO: regex)
      if ':' in error: error = error.split(':')[1].strip()
      error = error.replace('.', '').split()
      self._shift += 100 + (int(error[5]) - int(error[8])) / 1000
    else:
      self._shift = self._shift + 100

  def post(self, method, params, key, secret):
    request = { 'nonce' : int(time.time() + self._shift) * 1000, 'command' : method }
    if self._nonce >= request['nonce']:
      request['nonce'] = self._nonce + self._shift * 1000
    self._nonce = request['nonce']
    request.update(params)
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    headers = { 'Sign' : sign, 'Key' : key }
    return json.loads(urllib2.urlopen(urllib2.Request('https://poloniex.com/tradingApi', data, headers)).read())

  def cancel_orders(self, unit, key, secret):
    response = self.post('returnOpenOrders', { 'currencyPair' : "%s_NBT"%unit.upper() }, key, secret)
    if 'error' in response: return response
    for order in response:
      ret = self.post('cancelOrder', { 'currencyPair' : "%s_NBT"%unit.upper(), 'orderNumber' : order['orderNumber'] }, key, secret)
      if 'error' in ret:
        if isinstance(response,list): response = { 'error': "" }
        response['error'] += "," + ret['error']
    return response

  def place_order(self, unit, side, key, secret, amount, price):
    params = { 'currencyPair' : "%s_NBT"%unit.upper(), "rate" : price, "amount" : amount }
    response = self.post('buy' if side == 'bid' else 'sell', params, key, secret)
    if not 'error' in response:
      response['id'] = response['orderNumber']
    return response

  def get_balance(self, unit, key, secret):
    response = self.post('returnBalances', {}, key, secret)
    if not 'error' in response:
      response['balance'] = float(response[unit.upper()])
    return response

  def create_request(self, unit, key = None, secret = None):
    if not secret: return None, None
    request = { 'command' : 'returnOpenOrders', 'nonce' : int(time.time() + self._shift) * 1000,  'currencyPair' : "%s_NBT"%unit.upper() }
    if self._nonce >= request['nonce']:
      request['nonce'] = self._nonce + self._shift * 1000
    self._nonce = request['nonce']
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    return request, sign

  def validate_request(self, key, unit, data, sign):
    headers = { 'Sign' : sign, 'Key' : key }
    ret = urllib2.urlopen(urllib2.Request('https://poloniex.com/tradingApi', urllib.urlencode(data), headers))
    response = json.loads(ret.read())
    if 'error' in response: return response
    return [ {
      'id' : int(order['orderNumber']),
      'price' : float(order['rate']),
      'type' : 'ask' if order['type'] == 'sell' else 'bid',
      'amount' : float(order['amount']),
      } for order in response ]


class CCEDK(Exchange):
  def __init__(self):
    super(CCEDK, self).__init__('www.ccedk.com')
    self.pair_id = {}
    self.currency_id = {}
    failed = False
    while not self.pair_id or not self.currency_id:
      try:
        response = None
        if not self.pair_id:
          response = json.loads(urllib2.urlopen(urllib2.Request(
            'https://www.ccedk.com/api/v1/stats/marketdepthfull?' + urllib.urlencode({ 'nonce' : int(time.time() + self._shift) }))).read())
          for unit in response['response']['entities']:
            if unit['pair_name'][:4] == 'NBT/':
              self.pair_id[unit['pair_name'][4:]] = unit['pair_id']
        if not self.currency_id:
          response = json.loads(urllib2.urlopen(urllib2.Request(
            'https://www.ccedk.com/api/v1/currency/list?' + urllib.urlencode({ 'nonce' : int(time.time() + self._shift) }))).read())
          for unit in response['response']['entities']:
            self.currency_id[unit['iso'].lower()] = unit['currency_id']
      except:
        if response:
          self.adjust(",".join(response['errors'].values()))
          if failed: print >> sys.stderr, "could not retrieve ccedk ids, will adjust shift to", self._shift, "reason:", ",".join(response['errors'].values())
        else:
          print >> sys.stderr, "could not retrieve ccedk ids, server is unreachable"
        failed = True
        time.sleep(1)

  def __repr__(self): return "ccedk"

  def adjust(self, error):
    if "incorrect range" in error: #(TODO: regex)
      if ':' in error: error = error.split(':')[1].strip()
      try:
        minimum = int(error.strip().split()[-3].replace('`', ''))
        maximum = int(error.strip().split()[-1].replace('`', ''))
      except:
        super(CCEDK, self).adjust(error)
      else:
        current = int(time.time()) #int(error.split()[2].split('`')[3])
        if current < maximum:
          self._shift = (minimum + 2 * maximum) / 3  - current
        else:
          self._shift = (minimum + maximum) / 2 - current
    else:
        super(CCEDK, self).adjust(error)

  def post(self, method, params, key, secret):
    request = { 'nonce' : int(time.time()  + self._shift) }
    request.update(params)
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    headers = {"Content-type": "application/x-www-form-urlencoded", "Key": key, "Sign": sign}
    response = json.loads(urllib2.urlopen(urllib2.Request('https://www.ccedk.com/api/v1/' + method, data, headers)).read())
    if not response['response']:
      response['error'] = ",".join(response['errors'].values())
    return response

  def cancel_orders(self, unit, key, secret):
    response = self.post('order/list', {}, key, secret)
    if not response['response'] or not response['response']['entities']:
      return response
    for order in response['response']['entities']:
      if order['pair_id'] == self.pair_id[unit.upper()]:
        ret = self.post('order/cancel', { 'order_id' : order['order_id'] }, key, secret)
        if not ret['response']:
          if not 'error' in response: response['error'] = ""
          response['error'] += ",".join(ret['errors'].values())
    return response

  def place_order(self, unit, side, key, secret, amount, price):
    params = { "type" : 'buy' if side == 'bid' else 'sell',
               "price" : price,
               "pair_id" : int(self.pair_id[unit.upper()]),
               "volume" : amount }
    response = self.post('order/new', params, key, secret)
    if not 'error' in response:
      response['id'] = response['response']['entity']['order_id']
    return response

  def get_balance(self, unit, key, secret):
    params = { "currency_id" : self.currency_id[unit.lower()] }
    response = self.post('balance/info', params, key, secret)
    if not 'error' in response:
      response['balance'] = float(response['response']['entity']['balance'])
    return response

  def create_request(self, unit, key = None, secret = None):
    if not secret: return None, None
    request = { 'nonce' : int(time.time()  + self._shift) }
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    return request, sign

  def validate_request(self, key, unit, data, sign):
    headers = {"Content-type": "application/x-www-form-urlencoded", "Key": key, "Sign": sign}
    response = json.loads(urllib2.urlopen(urllib2.Request('https://www.ccedk.com/api/v1/order/list', urllib.urlencode(data), headers)).read())
    if not response['response']:
      response['error'] = ",".join(response['errors'].values())
      return response
    if not response['response']['entities']:
      response['response']['entities'] = []
    return [ {
      'id' : int(order['order_id']),
      'price' : float(order['price']),
      'type' : 'ask' if order['type'] == 'sell' else 'bid',
      'amount' : float(order['volume']),
      } for order in response['response']['entities'] if order['pair_id'] == self.pair_id[unit.upper()]]


class BitcoinCoId(Exchange):
  def __init__(self):
    super(BitcoinCoId, self).__init__('vip.bitcoin.co.id/tapi')
    self._nonce = 0
    self._shift = self.server_delta()

  def __repr__(self): return "bitcoincoid"

  def server_delta(self):
    try:
      response = json.loads(urllib2.urlopen(urllib2.Request('https://vip.bitcoin.co.id/api/summaries')).read())
      return float(response['tickers']['btc_idr']['server_time']) - time.time()
    except:
      return 0.0

  def adjust(self, error):
    if "Invalid nonce" in error: #(TODO: regex)
      self._shift += 25

  def post(self, method, params, key, secret):
    request = { 'nonce' : int(time.time() + self._shift) * 1000, 'method' : method }
    if self._nonce >= request['nonce']:
      request['nonce'] = self._nonce + 1000
    self._nonce = request['nonce']
    request.update(params)
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    headers = { 'Sign' : sign, 'Key' : key }
    response = json.loads(urllib2.urlopen(urllib2.Request('https://vip.bitcoin.co.id/tapi', data, headers)).read())
    return response

  def cancel_orders(self, unit, key, secret):
    response = self.post('openOrders', {'pair' : 'nbt_' + unit.lower()}, key, secret)
    if response['success'] == 0 or not response['return']['orders']: return response
    for order in response['return']['orders']:
      params = { 'pair' : 'nbt_' + unit.lower(), 'order_id' : order['order_id'], 'type' :  order['type'] }
      ret = self.post('cancelOrder', params, key, secret)
      if 'error' in ret:
        if not 'error' in response: response['error'] = ""
        response['error'] += "," + ret['error']
    return response

  def place_order(self, unit, side, key, secret, amount, price):
    params = { 'pair' : 'nbt_' + unit.lower(), 'type' : 'buy' if side == 'bid' else 'sell', 'price' : price }
    if side == 'bid':
      params[unit.lower()] = amount * price
    else:
      params['nbt'] = amount
      params[unit] = amount * price
    response = self.post('trade', params, key, secret)
    if response['success'] == 1:
      response['id'] = response['return']['order_id']
    return response

  def get_balance(self, unit, key, secret):
    response = self.post('getInfo', {}, key, secret)
    if response['success'] == 1:
      response['balance'] = float(response['return']['balance'][unit.lower()])
    return response

  def create_request(self, unit, key = None, secret = None):
    if not secret: return None, None
    request = { 'nonce' : int(time.time()  + self._shift) * 1000, 'pair' : 'nbt_' + unit.lower(), 'method' : 'openOrders' }
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    return request, sign

  def validate_request(self, key, unit, data, sign):
    headers = {"Key": key, "Sign": sign}
    response = json.loads(urllib2.urlopen(urllib2.Request('https://vip.bitcoin.co.id/tapi', urllib.urlencode(data), headers)).read())
    if response['success'] == 0:
      return response
    if not response['return']['orders']:
      response['return']['orders'] = []
    return [ {
      'id' : int(order['order_id']),
      'price' : float(order['price']),
      'type' : 'ask' if order['type'] == 'sell' else 'bid',
      'amount' : float(order['remain_' + (unit.lower() if order['type'] == 'buy' else 'nbt')]) / (float(order['price']) if order['type'] == 'buy' else 1.0),
      } for order in response['return']['orders']]


class BTER(Exchange):
  def __init__(self):
    super(BTER, self).__init__('data.bter.com')
    #self._nonce = 0

  def __repr__(self): return "bter"

  def adjust(self, error):
    print error

  def https_request(self, method, params, headers = None):
    if not headers: headers = {}
    connection = httplib.HTTPSConnection(self._domain, timeout=60)
    connection.request('POST', '/api/1/private/' + method, params, headers)
    response = connection.getresponse().read()
    return json.loads(response)

  def post(self, method, params, key, secret):
    #request = { 'nonce' : int(time.time() + self._shift) }
    #if self._nonce >= request['nonce']:
    #  request['nonce'] = self._nonce + 1
    #self._nonce = request['nonce']
    #request.update(params)
    data = urllib.urlencode(params)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    headers = { 'Sign' : sign, 'Key' : key, "Content-type": "application/x-www-form-urlencoded" }
    return self.https_request(method, data, headers)

  def cancel_orders(self, unit, key, secret):
    response = self.post('orderlist', {}, key, secret)
    if not response['result']:
      response['error'] = response['msg']
      return response
    if not response['orders']: response['orders'] = []
    for order in response['orders']:
      params = { 'order_id' : order['id'] }
      ret = self.post('cancelorder', params, key, secret)
      if not ret['result']:
        if not 'error' in response: response['error'] = ""
        response['error'] += "," + ret['msg']
    return response

  def place_order(self, unit, side, key, secret, amount, price):
    params = { 'pair' : 'nbt_' + unit.lower(), 'type' : 'buy' if side == 'bid' else 'sell', 'rate' : price, 'amount' : amount }
    response = self.post('placeorder', params, key, secret)
    if response['result']:
      response['id'] = response['order_id']
    else:
      response['error'] = response['msg']
    return response

  def get_balance(self, unit, key, secret):
    response = self.post('getfunds', {}, key, secret)
    if response['result']:
      if unit.upper() in response['available_funds']:
        response['balance'] = float(response['available_funds'][unit.upper()])
      else:
        response['balance'] = 0.0
    else: response['error'] = response['msg']
    return response

  def create_request(self, unit, key = None, secret = None):
    if not secret: return None, None
    request = {} #'nonce' : int(time.time() + self._shift) }
    data = urllib.urlencode(request)
    sign = hmac.new(secret, data, hashlib.sha512).hexdigest()
    return request, sign

  def validate_request(self, key, unit, data, sign):
    headers = { 'Sign' : sign, 'Key' : key, "Content-type": "application/x-www-form-urlencoded" }
    response = self.https_request('orderlist', urllib.urlencode(data), headers)
    #response = json.loads(urllib2.urlopen(urllib2.Request('https://bter.com/api/1/private/orderlist', urllib.urlencode(data), headers)).read())
    if not response['result']:
      response['error'] = response['msg']
      return response
    if not response['orders']:
      response['orders'] = []
    return [ {
      'id' : int(order['id']),
      'price' : float(order['rate']),
      'type' : 'ask' if order['buy_type'] == unit else 'bid',
      'amount' : float(order['amount']),
      } for order in response['orders'] if order['pair'] == 'nbt_' + unit.lower() ]