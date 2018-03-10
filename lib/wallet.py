# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Wallet classes:
#   - Imported_Wallet: imported address, no keystore
#   - Standard_Wallet: one keystore, P2PKH
#   - Multisig_Wallet: several keystores, P2SH


import os
import threading
import random
import time
import json
import copy
import errno
import traceback
from functools import partial
from collections import defaultdict
from numbers import Number
from decimal import Decimal

import sys

from .i18n import _
from .util import (NotEnoughFunds, PrintError, UserCancelled, profiler,
                   format_satoshis, NoDynamicFeeEstimates, TimeoutException)

from .bitcoin import *
from .version import *
from .keystore import load_keystore, Hardware_KeyStore
from .storage import multisig_type, STO_EV_PLAINTEXT, STO_EV_USER_PW, STO_EV_XPUB_PW

from . import transaction
from .transaction import Transaction
from .plugins import run_hook
from . import bitcoin
from . import coinchooser
from .synchronizer import Synchronizer
from .verifier import SPV

from . import paymentrequest
from .paymentrequest import PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED
from .paymentrequest import InvoiceStore
from .contacts import Contacts

TX_STATUS = [
    _('Unconfirmed'),
    _('Unconfirmed parent'),
    _('Not Verified'),
    _('Local'),
]

TX_HEIGHT_LOCAL = -2
TX_HEIGHT_UNCONF_PARENT = -1
TX_HEIGHT_UNCONFIRMED = 0


def relayfee(network):
    from .simple_config import FEERATE_DEFAULT_RELAY
    MAX_RELAY_FEE = 10 * FEERATE_DEFAULT_RELAY
    f = network.relay_fee if network and network.relay_fee else FEERATE_DEFAULT_RELAY
    return min(f, MAX_RELAY_FEE)

def dust_threshold(network):
    # Change <= dust threshold is added to the tx fee
    return 182 * 3 * relayfee(network) / 1000


def append_utxos_to_inputs(inputs, network, pubkey, txin_type, imax):
    if txin_type != 'p2pk':
        address = bitcoin.pubkey_to_address(txin_type, pubkey)
        sh = bitcoin.address_to_scripthash(address)
    else:
        script = bitcoin.public_key_to_p2pk_script(pubkey)
        sh = bitcoin.script_to_scripthash(script)
        address = '(pubkey)'
    u = network.synchronous_get(('blockchain.scripthash.listunspent', [sh]))
    for item in u:
        if len(inputs) >= imax:
            break
        item['address'] = address
        item['type'] = txin_type
        item['prevout_hash'] = item['tx_hash']
        item['prevout_n'] = item['tx_pos']
        item['pubkeys'] = [pubkey]
        item['x_pubkeys'] = [pubkey]
        item['signatures'] = [None]
        item['num_sig'] = 1
        inputs.append(item)

def sweep_preparations(privkeys, network, imax=100):

    def find_utxos_for_privkey(txin_type, privkey, compressed):
        pubkey = bitcoin.public_key_from_private_key(privkey, compressed)
        append_utxos_to_inputs(inputs, network, pubkey, txin_type, imax)
        keypairs[pubkey] = privkey, compressed
    inputs = []
    keypairs = {}
    for sec in privkeys:
        txin_type, privkey, compressed = bitcoin.deserialize_privkey(sec)
        find_utxos_for_privkey(txin_type, privkey, compressed)
        # do other lookups to increase support coverage
        if is_minikey(sec):
            # minikeys don't have a compressed byte
            # we lookup both compressed and uncompressed pubkeys
            find_utxos_for_privkey(txin_type, privkey, not compressed)
        elif txin_type == 'p2pkh':
            # WIF serialization does not distinguish p2pkh and p2pk
            # we also search for pay-to-pubkey outputs
            find_utxos_for_privkey('p2pk', privkey, compressed)
    if not inputs:
        raise BaseException(_('No inputs found. (Note that inputs need to be confirmed)'))
    return inputs, keypairs


def sweep(privkeys, network, config, recipient, fee=None, imax=100):
    inputs, keypairs = sweep_preparations(privkeys, network, imax)
    total = sum(i.get('value') for i in inputs)
    if fee is None:
        outputs = [(TYPE_ADDRESS, recipient, total)]
        tx = Transaction.from_io(inputs, outputs)
        fee = config.estimate_fee(tx.estimated_size())
    if total - fee < 0:
        raise BaseException(_('Not enough funds on address.') + '\nTotal: %d satoshis\nFee: %d'%(total, fee))
    if total - fee < dust_threshold(network):
        raise BaseException(_('Not enough funds on address.') + '\nTotal: %d satoshis\nFee: %d\nDust Threshold: %d'%(total, fee, dust_threshold(network)))

    outputs = [(TYPE_ADDRESS, recipient, total - fee)]
    locktime = network.get_local_height()

    tx = Transaction.from_io(inputs, outputs, locktime=locktime)
    tx.BIP_LI01_sort()
    tx.set_rbf(True)
    tx.sign(keypairs)
    return tx


class AddTransactionException(Exception):
    pass


class UnrelatedTransactionException(AddTransactionException):
    def __str__(self):
        return _("Transaction is unrelated to this wallet.")


class NotIsMineTransactionException(AddTransactionException):
    def __str__(self):
        return _("Only transactions with inputs owned by the wallet can be added.")


class Abstract_Wallet(PrintError):
    """
    Wallet classes are created to handle various address generation methods.
    Completion states (watching-only, single account, no seed, etc) are handled inside classes.
    """

    max_change_outputs = 3

    def __init__(self, storage):
        self.electrum_version = ELECTRUM_VERSION
        self.storage = storage
        self.network = None
        # verifier (SPV) and synchronizer are started in start_threads
        self.synchronizer = None
        self.verifier = None

        self.gap_limit_for_change = 6 # constant
        # saved fields
        self.use_change            = storage.get('use_change', True)
        self.multiple_change       = storage.get('multiple_change', False)
        self.labels                = storage.get('labels', {})
        self.frozen_addresses      = set(storage.get('frozen_addresses',[]))
        self.history               = storage.get('addr_history',{})        # address -> list(txid, height)
        self.fiat_value            = storage.get('fiat_value', {})

        self.load_keystore()
        self.load_addresses()
        self.load_transactions()
        self.build_spent_outpoints()

        self.test_addresses_sanity()

        # load requests
        self.receive_requests = self.storage.get('payment_requests', {})

        # Transactions pending verification.  A map from tx hash to transaction
        # height.  Access is not contended so no lock is needed.
        self.unverified_tx = defaultdict(int)

        # Verified transactions.  Each value is a (height, timestamp, block_pos) tuple.  Access with self.lock.
        self.verified_tx = storage.get('verified_tx3', {})

        # there is a difference between wallet.up_to_date and interface.is_up_to_date()
        # interface.is_up_to_date() returns true when all requests have been answered and processed
        # wallet.up_to_date is true when the wallet is synchronized (stronger requirement)
        self.up_to_date = False

        # locks: if you need to take multiple ones, acquire them in the order they are defined here!
        self.lock = threading.RLock()
        self.transaction_lock = threading.RLock()

        self.check_history()

        # save wallet type the first time
        if self.storage.get('wallet_type') is None:
            self.storage.put('wallet_type', self.wallet_type)

        # invoices and contacts
        self.invoices = InvoiceStore(self.storage)
        self.contacts = Contacts(self.storage)

        self.coin_price_cache = {}


    def diagnostic_name(self):
        return self.basename()

    def __str__(self):
        return self.basename()

    def get_master_public_key(self):
        return None

    @profiler
    def load_transactions(self):
        self.txi = self.storage.get('txi', {})
        self.txo = self.storage.get('txo', {})
        self.tx_fees = self.storage.get('tx_fees', {})
        self.pruned_txo = self.storage.get('pruned_txo', {})
        tx_list = self.storage.get('transactions', {})
        self.transactions = {}
        for tx_hash, raw in tx_list.items():
            tx = Transaction(raw)
            self.transactions[tx_hash] = tx
            if self.txi.get(tx_hash) is None and self.txo.get(tx_hash) is None \
                    and (tx_hash not in self.pruned_txo.values()):
                self.print_error("removing unreferenced tx", tx_hash)
                self.transactions.pop(tx_hash)

    @profiler
    def save_transactions(self, write=False):
        with self.transaction_lock:
            tx = {}
            for k,v in self.transactions.items():
                tx[k] = str(v)
            self.storage.put('transactions', tx)
            self.storage.put('txi', self.txi)
            self.storage.put('txo', self.txo)
            self.storage.put('tx_fees', self.tx_fees)
            self.storage.put('pruned_txo', self.pruned_txo)
            self.storage.put('addr_history', self.history)
            if write:
                self.storage.write()

    def clear_history(self):
        with self.lock:
            with self.transaction_lock:
                self.txi = {}
                self.txo = {}
                self.tx_fees = {}
                self.pruned_txo = {}
                self.spent_outpoints = {}
                self.history = {}
                self.verified_tx = {}
                self.transactions = {}
                self.save_transactions()

    @profiler
    def build_spent_outpoints(self):
        self.spent_outpoints = {}
        for txid, items in self.txi.items():
            for addr, l in items.items():
                for ser, v in l:
                    self.spent_outpoints[ser] = txid

    @profiler
    def check_history(self):
        save = False

        hist_addrs_mine = list(filter(lambda k: self.is_mine(k), self.history.keys()))
        hist_addrs_not_mine = list(filter(lambda k: not self.is_mine(k), self.history.keys()))

        for addr in hist_addrs_not_mine:
            self.history.pop(addr)
            save = True

        for addr in hist_addrs_mine:
            hist = self.history[addr]

            for tx_hash, tx_height in hist:
                if tx_hash in self.pruned_txo.values() or self.txi.get(tx_hash) or self.txo.get(tx_hash):
                    continue
                tx = self.transactions.get(tx_hash)
                if tx is not None:
                    self.add_transaction(tx_hash, tx)
                    save = True
        if save:
            self.save_transactions()

    def basename(self):
        return os.path.basename(self.storage.path)

    def save_addresses(self):
        self.storage.put('addresses', {'receiving':self.receiving_addresses, 'change':self.change_addresses})

    def load_addresses(self):
        d = self.storage.get('addresses', {})
        if type(d) != dict: d={}
        self.receiving_addresses = d.get('receiving', [])
        self.change_addresses = d.get('change', [])

    def test_addresses_sanity(self):
        addrs = self.get_receiving_addresses()
        if len(addrs) > 0:
            if not bitcoin.is_address(addrs[0]):
                raise Exception('The addresses in this wallet are not Litecoin addresses.')

    def synchronize(self):
        pass

    def is_deterministic(self):
        return self.keystore.is_deterministic()

    def set_up_to_date(self, up_to_date):
        with self.lock:
            self.up_to_date = up_to_date
        if up_to_date:
            self.save_transactions(write=True)

    def is_up_to_date(self):
        with self.lock: return self.up_to_date

    def set_label(self, name, text = None):
        changed = False
        old_text = self.labels.get(name)
        if text:
            text = text.replace("\n", " ")
            if old_text != text:
                self.labels[name] = text
                changed = True
        else:
            if old_text:
                self.labels.pop(name)
                changed = True
        if changed:
            run_hook('set_label', self, name, text)
            self.storage.put('labels', self.labels)
        return changed

    def set_fiat_value(self, txid, ccy, text):
        if txid not in self.transactions:
            return
        if not text:
            d = self.fiat_value.get(ccy, {})
            if d and txid in d:
                d.pop(txid)
            else:
                return
        else:
            try:
                Decimal(text)
            except:
                return
        if ccy not in self.fiat_value:
            self.fiat_value[ccy] = {}
        self.fiat_value[ccy][txid] = text
        self.storage.put('fiat_value', self.fiat_value)

    def get_fiat_value(self, txid, ccy):
        fiat_value = self.fiat_value.get(ccy, {}).get(txid)
        try:
            return Decimal(fiat_value)
        except:
            return

    def is_mine(self, address):
        return address in self.get_addresses()

    def is_change(self, address):
        if not self.is_mine(address):
            return False
        return self.get_address_index(address)[0]

    def get_address_index(self, address):
        raise NotImplementedError()

    def get_redeem_script(self, address):
        return None

    def export_private_key(self, address, password):
        if self.is_watching_only():
            return []
        index = self.get_address_index(address)
        pk, compressed = self.keystore.get_private_key(index, password)
        txin_type = self.get_txin_type(address)
        redeem_script = self.get_redeem_script(address)
        serialized_privkey = bitcoin.serialize_privkey(pk, compressed, txin_type)
        return serialized_privkey, redeem_script

    def get_public_keys(self, address):
        return [self.get_public_key(address)]

    def add_unverified_tx(self, tx_hash, tx_height):
        if tx_height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT) \
                and tx_hash in self.verified_tx:
            self.verified_tx.pop(tx_hash)
            if self.verifier:
                self.verifier.merkle_roots.pop(tx_hash, None)

        # tx will be verified only if height > 0
        if tx_hash not in self.verified_tx:
            self.unverified_tx[tx_hash] = tx_height

    def add_verified_tx(self, tx_hash, info):
        # Remove from the unverified map and add to the verified map and
        self.unverified_tx.pop(tx_hash, None)
        with self.lock:
            self.verified_tx[tx_hash] = info  # (tx_height, timestamp, pos)
        height, conf, timestamp = self.get_tx_height(tx_hash)
        self.network.trigger_callback('verified', tx_hash, height, conf, timestamp)

    def get_unverified_txs(self):
        '''Returns a map from tx hash to transaction height'''
        return self.unverified_tx

    def undo_verifications(self, blockchain, height):
        '''Used by the verifier when a reorg has happened'''
        txs = set()
        with self.lock:
            for tx_hash, item in list(self.verified_tx.items()):
                tx_height, timestamp, pos = item
                if tx_height >= height:
                    header = blockchain.read_header(tx_height)
                    # fixme: use block hash, not timestamp
                    if not header or header.get('timestamp') != timestamp:
                        self.verified_tx.pop(tx_hash, None)
                        txs.add(tx_hash)
        return txs

    def get_local_height(self):
        """ return last known height if we are offline """
        return self.network.get_local_height() if self.network else self.storage.get('stored_height', 0)

    def get_tx_height(self, tx_hash):
        """ Given a transaction, returns (height, conf, timestamp) """
        with self.lock:
            if tx_hash in self.verified_tx:
                height, timestamp, pos = self.verified_tx[tx_hash]
                conf = max(self.get_local_height() - height + 1, 0)
                return height, conf, timestamp
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return height, 0, None
            else:
                # local transaction
                return TX_HEIGHT_LOCAL, 0, None

    def get_txpos(self, tx_hash):
        "return position, even if the tx is unverified"
        with self.lock:
            if tx_hash in self.verified_tx:
                height, timestamp, pos = self.verified_tx[tx_hash]
                return height, pos
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return (height, 0) if height > 0 else ((1e9 - height), 0)
            else:
                return (1e9+1, 0)

    def is_found(self):
        return self.history.values() != [[]] * len(self.history)

    def get_num_tx(self, address):
        """ return number of transactions where address is involved """
        return len(self.history.get(address, []))

    def get_tx_delta(self, tx_hash, address):
        "effect of tx on address"
        # pruned
        if tx_hash in self.pruned_txo.values():
            return None
        delta = 0
        # substract the value of coins sent from address
        d = self.txi.get(tx_hash, {}).get(address, [])
        for n, v in d:
            delta -= v
        # add the value of the coins received at address
        d = self.txo.get(tx_hash, {}).get(address, [])
        for n, v, cb in d:
            delta += v
        return delta

    def get_tx_value(self, txid):
        " effect of tx on the entire domain"
        delta = 0
        for addr, d in self.txi.get(txid, {}).items():
            for n, v in d:
                delta -= v
        for addr, d in self.txo.get(txid, {}).items():
            for n, v, cb in d:
                delta += v
        return delta

    def get_wallet_delta(self, tx):
        """ effect of tx on wallet """
        addresses = self.get_addresses()
        is_relevant = False
        is_mine = False
        is_pruned = False
        is_partial = False
        v_in = v_out = v_out_mine = 0
        for item in tx.inputs():
            addr = item.get('address')
            if addr in addresses:
                is_mine = True
                is_relevant = True
                d = self.txo.get(item['prevout_hash'], {}).get(addr, [])
                for n, v, cb in d:
                    if n == item['prevout_n']:
                        value = v
                        break
                else:
                    value = None
                if value is None:
                    is_pruned = True
                else:
                    v_in += value
            else:
                is_partial = True
        if not is_mine:
            is_partial = False
        for addr, value in tx.get_outputs():
            v_out += value
            if addr in addresses:
                v_out_mine += value
                is_relevant = True
        if is_pruned:
            # some inputs are mine:
            fee = None
            if is_mine:
                v = v_out_mine - v_out
            else:
                # no input is mine
                v = v_out_mine
        else:
            v = v_out_mine - v_in
            if is_partial:
                # some inputs are mine, but not all
                fee = None
            else:
                # all inputs are mine
                fee = v_in - v_out
        if not is_mine:
            fee = None
        return is_relevant, is_mine, v, fee

    def get_tx_info(self, tx):
        is_relevant, is_mine, v, fee = self.get_wallet_delta(tx)
        exp_n = None
        can_broadcast = False
        can_bump = False
        label = ''
        height = conf = timestamp = None
        tx_hash = tx.txid()
        if tx.is_complete():
            if tx_hash in self.transactions.keys():
                label = self.get_label(tx_hash)
                height, conf, timestamp = self.get_tx_height(tx_hash)
                if height > 0:
                    if conf:
                        status = _("{} confirmations").format(conf)
                    else:
                        status = _('Not verified')
                elif height in (TX_HEIGHT_UNCONF_PARENT, TX_HEIGHT_UNCONFIRMED):
                    status = _('Unconfirmed')
                    if fee is None:
                        fee = self.tx_fees.get(tx_hash)
                    if fee and self.network.config.has_fee_mempool():
                        size = tx.estimated_size()
                        fee_per_byte = fee / size
                        exp_n = self.network.config.fee_to_depth(fee_per_byte)
                    can_bump = is_mine and not tx.is_final()
                else:
                    status = _('Local')
                    can_broadcast = self.network is not None
            else:
                status = _("Signed")
                can_broadcast = self.network is not None
        else:
            s, r = tx.signature_count()
            status = _("Unsigned") if s == 0 else _('Partially signed') + ' (%d/%d)'%(s,r)

        if is_relevant:
            if is_mine:
                if fee is not None:
                    amount = v + fee
                else:
                    amount = v
            else:
                amount = v
        else:
            amount = None

        return tx_hash, status, label, can_broadcast, can_bump, amount, fee, height, conf, timestamp, exp_n

    def get_addr_io(self, address):
        h = self.get_address_history(address)
        received = {}
        sent = {}
        for tx_hash, height in h:
            l = self.txo.get(tx_hash, {}).get(address, [])
            for n, v, is_cb in l:
                received[tx_hash + ':%d'%n] = (height, v, is_cb)
        for tx_hash, height in h:
            l = self.txi.get(tx_hash, {}).get(address, [])
            for txi, v in l:
                sent[txi] = height
        return received, sent

    def get_addr_utxo(self, address):
        coins, spent = self.get_addr_io(address)
        for txi in spent:
            coins.pop(txi)
        out = {}
        for txo, v in coins.items():
            tx_height, value, is_cb = v
            prevout_hash, prevout_n = txo.split(':')
            x = {
                'address':address,
                'value':value,
                'prevout_n':int(prevout_n),
                'prevout_hash':prevout_hash,
                'height':tx_height,
                'coinbase':is_cb
            }
            out[txo] = x
        return out

    # return the total amount ever received by an address
    def get_addr_received(self, address):
        received, sent = self.get_addr_io(address)
        return sum([v for height, v, is_cb in received.values()])

    # return the balance of a bitcoin address: confirmed and matured, unconfirmed, unmatured
    def get_addr_balance(self, address):
        received, sent = self.get_addr_io(address)
        c = u = x = 0
        for txo, (tx_height, v, is_cb) in received.items():
            if is_cb and tx_height + COINBASE_MATURITY > self.get_local_height():
                x += v
            elif tx_height > 0:
                c += v
            else:
                u += v
            if txo in sent:
                if sent[txo] > 0:
                    c -= v
                else:
                    u -= v
        return c, u, x

    def get_spendable_coins(self, domain, config):
        confirmed_only = config.get('confirmed_only', False)
        return self.get_utxos(domain, exclude_frozen=True, mature=True, confirmed_only=confirmed_only)

    def get_utxos(self, domain = None, exclude_frozen = False, mature = False, confirmed_only = False):
        coins = []
        if domain is None:
            domain = self.get_addresses()
        if exclude_frozen:
            domain = set(domain) - self.frozen_addresses
        for addr in domain:
            utxos = self.get_addr_utxo(addr)
            for x in utxos.values():
                if confirmed_only and x['height'] <= 0:
                    continue
                if mature and x['coinbase'] and x['height'] + COINBASE_MATURITY > self.get_local_height():
                    continue
                coins.append(x)
                continue
        return coins

    def dummy_address(self):
        return self.get_receiving_addresses()[0]

    def get_addresses(self):
        out = []
        out += self.get_receiving_addresses()
        out += self.get_change_addresses()
        return out

    def get_frozen_balance(self):
        return self.get_balance(self.frozen_addresses)

    def get_balance(self, domain=None):
        if domain is None:
            domain = self.get_addresses()
        cc = uu = xx = 0
        for addr in domain:
            c, u, x = self.get_addr_balance(addr)
            cc += c
            uu += u
            xx += x
        return cc, uu, xx

    def get_address_history(self, addr):
        h = []
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            for tx_hash in self.transactions:
                if addr in self.txi.get(tx_hash, []) or addr in self.txo.get(tx_hash, []):
                    tx_height = self.get_tx_height(tx_hash)[0]
                    h.append((tx_hash, tx_height))
        return h

    def get_txin_address(self, txi):
        addr = txi.get('address')
        if addr != "(pubkey)":
            return addr
        prevout_hash = txi.get('prevout_hash')
        prevout_n = txi.get('prevout_n')
        dd = self.txo.get(prevout_hash, {})
        for addr, l in dd.items():
            for n, v, is_cb in l:
                if n == prevout_n:
                    self.print_error("found pay-to-pubkey address:", addr)
                    return addr

    def get_txout_address(self, txo):
        _type, x, v = txo
        if _type == TYPE_ADDRESS:
            addr = x
        elif _type == TYPE_PUBKEY:
            addr = bitcoin.public_key_to_p2pkh(bfh(x))
        else:
            addr = None
        return addr

    def get_conflicting_transactions(self, tx):
        """Returns a set of transaction hashes from the wallet history that are
        directly conflicting with tx, i.e. they have common outpoints being
        spent with tx. If the tx is already in wallet history, that will not be
        reported as a conflict.
        """
        conflicting_txns = set()
        with self.transaction_lock:
            for txi in tx.inputs():
                ser = Transaction.get_outpoint_from_txin(txi)
                if ser is None:
                    continue
                spending_tx_hash = self.spent_outpoints.get(ser, None)
                if spending_tx_hash is None:
                    continue
                # this outpoint (ser) has already been spent, by spending_tx
                assert spending_tx_hash in self.transactions
                conflicting_txns |= {spending_tx_hash}
            txid = tx.txid()
            if txid in conflicting_txns:
                # this tx is already in history, so it conflicts with itself
                if len(conflicting_txns) > 1:
                    raise Exception('Found conflicting transactions already in wallet history.')
                conflicting_txns -= {txid}
            return conflicting_txns

    def add_transaction(self, tx_hash, tx):
        assert tx_hash, tx_hash
        assert tx, tx
        assert tx.is_complete()
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            # NOTE: returning if tx in self.transactions might seem like a good idea
            # BUT we track is_mine inputs in a txn, and during subsequent calls
            # of add_transaction tx, we might learn of more-and-more inputs of
            # being is_mine, as we roll the gap_limit forward
            is_coinbase = tx.inputs()[0]['type'] == 'coinbase'
            tx_height = self.get_tx_height(tx_hash)[0]
            is_mine = any([self.is_mine(txin['address']) for txin in tx.inputs()])
            # do not save if tx is local and not mine
            if tx_height == TX_HEIGHT_LOCAL and not is_mine:
                # FIXME the test here should be for "not all is_mine"; cannot detect conflict in some cases
                raise NotIsMineTransactionException()
            # raise exception if unrelated to wallet
            is_for_me = any([self.is_mine(self.get_txout_address(txo)) for txo in tx.outputs()])
            if not is_mine and not is_for_me:
                raise UnrelatedTransactionException()
            # Find all conflicting transactions.
            # In case of a conflict,
            #     1. confirmed > mempool > local
            #     2. this new txn has priority over existing ones
            # When this method exits, there must NOT be any conflict, so
            # either keep this txn and remove all conflicting (along with dependencies)
            #     or drop this txn
            conflicting_txns = self.get_conflicting_transactions(tx)
            if conflicting_txns:
                existing_mempool_txn = any(
                    self.get_tx_height(tx_hash2)[0] in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT)
                    for tx_hash2 in conflicting_txns)
                existing_confirmed_txn = any(
                    self.get_tx_height(tx_hash2)[0] > 0
                    for tx_hash2 in conflicting_txns)
                if existing_confirmed_txn and tx_height <= 0:
                    # this is a non-confirmed tx that conflicts with confirmed txns; drop.
                    return False
                if existing_mempool_txn and tx_height == TX_HEIGHT_LOCAL:
                    # this is a local tx that conflicts with non-local txns; drop.
                    return False
                # keep this txn and remove all conflicting
                to_remove = set()
                to_remove |= conflicting_txns
                for conflicting_tx_hash in conflicting_txns:
                    to_remove |= self.get_depending_transactions(conflicting_tx_hash)
                for tx_hash2 in to_remove:
                    self.remove_transaction(tx_hash2)
            # add inputs
            self.txi[tx_hash] = d = {}
            for txi in tx.inputs():
                addr = self.get_txin_address(txi)
                if txi['type'] != 'coinbase':
                    prevout_hash = txi['prevout_hash']
                    prevout_n = txi['prevout_n']
                    ser = prevout_hash + ':%d'%prevout_n
                if addr and self.is_mine(addr):
                    # we only track is_mine spends
                    self.spent_outpoints[ser] = tx_hash
                    # find value from prev output
                    dd = self.txo.get(prevout_hash, {})
                    for n, v, is_cb in dd.get(addr, []):
                        if n == prevout_n:
                            if d.get(addr) is None:
                                d[addr] = []
                            d[addr].append((ser, v))
                            break
                    else:
                        self.pruned_txo[ser] = tx_hash
            # add outputs
            self.txo[tx_hash] = d = {}
            for n, txo in enumerate(tx.outputs()):
                v = txo[2]
                ser = tx_hash + ':%d'%n
                addr = self.get_txout_address(txo)
                if addr and self.is_mine(addr):
                    if d.get(addr) is None:
                        d[addr] = []
                    d[addr].append((n, v, is_coinbase))
                # give v to txi that spends me
                next_tx = self.pruned_txo.get(ser)
                if next_tx is not None:
                    self.pruned_txo.pop(ser)
                    dd = self.txi.get(next_tx, {})
                    if dd.get(addr) is None:
                        dd[addr] = []
                    dd[addr].append((ser, v))
            # save
            self.transactions[tx_hash] = tx
            return True

    def remove_transaction(self, tx_hash):

        with self.transaction_lock:
            self.print_error("removing tx from history", tx_hash)
            self.transactions.pop(tx_hash, None)
            # undo spent_outpoints that are in txi
            for addr, l in self.txi[tx_hash].items():
                for ser, v in l:
                    self.spent_outpoints.pop(ser, None)
            # undo spent_outpoints that are in pruned_txo
            for ser, hh in list(self.pruned_txo.items()):
                if hh == tx_hash:
                    self.spent_outpoints.pop(ser)
                    self.pruned_txo.pop(ser)

            # add tx to pruned_txo, and undo the txi addition
            for next_tx, dd in self.txi.items():
                for addr, l in list(dd.items()):
                    ll = l[:]
                    for item in ll:
                        ser, v = item
                        prev_hash, prev_n = ser.split(':')
                        if prev_hash == tx_hash:
                            l.remove(item)
                            self.pruned_txo[ser] = next_tx
                    if l == []:
                        dd.pop(addr)
                    else:
                        dd[addr] = l
            try:
                self.txi.pop(tx_hash)
                self.txo.pop(tx_hash)
            except KeyError:
                self.print_error("tx was not in history", tx_hash)

    def receive_tx_callback(self, tx_hash, tx, tx_height):
        self.add_unverified_tx(tx_hash, tx_height)
        self.add_transaction(tx_hash, tx)

    def receive_history_callback(self, addr, hist, tx_fees):
        with self.lock:
            old_hist = self.get_address_history(addr)
            for tx_hash, height in old_hist:
                if (tx_hash, height) not in hist:
                    # make tx local
                    self.unverified_tx.pop(tx_hash, None)
                    self.verified_tx.pop(tx_hash, None)
                    if self.verifier:
                        self.verifier.merkle_roots.pop(tx_hash, None)
                    # but remove completely if not is_mine
                    if self.txi[tx_hash] == {}:
                        # FIXME the test here should be for "not all is_mine"; cannot detect conflict in some cases
                        self.remove_transaction(tx_hash)
            self.history[addr] = hist

        for tx_hash, tx_height in hist:
            # add it in case it was previously unconfirmed
            self.add_unverified_tx(tx_hash, tx_height)
            # if addr is new, we have to recompute txi and txo
            tx = self.transactions.get(tx_hash)
            if tx is not None and self.txi.get(tx_hash, {}).get(addr) is None and self.txo.get(tx_hash, {}).get(addr) is None:
                self.add_transaction(tx_hash, tx)

        # Store fees
        self.tx_fees.update(tx_fees)

    def get_history(self, domain=None):
        # get domain
        if domain is None:
            domain = self.get_addresses()
        # 1. Get the history of each address in the domain, maintain the
        #    delta of a tx as the sum of its deltas on domain addresses
        tx_deltas = defaultdict(int)
        for addr in domain:
            h = self.get_address_history(addr)
            for tx_hash, height in h:
                delta = self.get_tx_delta(tx_hash, addr)
                if delta is None or tx_deltas[tx_hash] is None:
                    tx_deltas[tx_hash] = None
                else:
                    tx_deltas[tx_hash] += delta

        # 2. create sorted history
        history = []
        for tx_hash in tx_deltas:
            delta = tx_deltas[tx_hash]
            height, conf, timestamp = self.get_tx_height(tx_hash)
            history.append((tx_hash, height, conf, timestamp, delta))
        history.sort(key = lambda x: self.get_txpos(x[0]))
        history.reverse()

        # 3. add balance
        c, u, x = self.get_balance(domain)
        balance = c + u + x
        h2 = []
        for tx_hash, height, conf, timestamp, delta in history:
            h2.append((tx_hash, height, conf, timestamp, delta, balance))
            if balance is None or delta is None:
                balance = None
            else:
                balance -= delta
        h2.reverse()

        # fixme: this may happen if history is incomplete
        if balance not in [None, 0]:
            self.print_error("Error: history not synchronized")
            return []

        return h2

    def balance_at_timestamp(self, domain, target_timestamp):
        h = self.get_history(domain)
        for tx_hash, height, conf, timestamp, value, balance in h:
            if timestamp > target_timestamp:
                return balance - value
        # return last balance
        return balance

    @profiler
    def get_full_history(self, domain=None, from_timestamp=None, to_timestamp=None, fx=None, show_addresses=False):
        from .util import timestamp_to_datetime, Satoshis, Fiat
        out = []
        income = 0
        expenditures = 0
        capital_gains = Decimal(0)
        fiat_income = Decimal(0)
        fiat_expenditures = Decimal(0)
        h = self.get_history(domain)
        for tx_hash, height, conf, timestamp, value, balance in h:
            if from_timestamp and (timestamp or time.time()) < from_timestamp:
                continue
            if to_timestamp and (timestamp or time.time()) >= to_timestamp:
                continue
            item = {
                'txid':tx_hash,
                'height':height,
                'confirmations':conf,
                'timestamp':timestamp,
                'value': Satoshis(value),
                'balance': Satoshis(balance)
            }
            item['date'] = timestamp_to_datetime(timestamp)
            item['label'] = self.get_label(tx_hash)
            if show_addresses:
                tx = self.transactions.get(tx_hash)
                tx.deserialize()
                input_addresses = []
                output_addresses = []
                for x in tx.inputs():
                    if x['type'] == 'coinbase': continue
                    addr = self.get_txin_address(x)
                    if addr is None:
                        continue
                    input_addresses.append(addr)
                for addr, v in tx.get_outputs():
                    output_addresses.append(addr)
                item['input_addresses'] = input_addresses
                item['output_addresses'] = output_addresses
            # value may be None if wallet is not fully synchronized
            if value is None:
                continue
            # fixme: use in and out values
            if value < 0:
                expenditures += -value
            else:
                income += value
            # fiat computations
            if fx and fx.is_enabled():
                date = timestamp_to_datetime(timestamp)
                fiat_value = self.get_fiat_value(tx_hash, fx.ccy)
                fiat_default = fiat_value is None
                fiat_value = fiat_value if fiat_value is not None else value / Decimal(COIN) * self.price_at_timestamp(tx_hash, fx.timestamp_rate)
                item['fiat_value'] = Fiat(fiat_value, fx.ccy)
                item['fiat_default'] = fiat_default
                if value < 0:
                    acquisition_price = - value / Decimal(COIN) * self.average_price(tx_hash, fx.timestamp_rate, fx.ccy)
                    liquidation_price = - fiat_value
                    item['acquisition_price'] = Fiat(acquisition_price, fx.ccy)
                    cg = liquidation_price - acquisition_price
                    item['capital_gain'] = Fiat(cg, fx.ccy)
                    capital_gains += cg
                    fiat_expenditures += -fiat_value
                else:
                    fiat_income += fiat_value
            out.append(item)
        # add summary
        if out:
            b, v = out[0]['balance'].value, out[0]['value'].value
            start_balance = None if b is None or v is None else b - v
            end_balance = out[-1]['balance'].value
            if from_timestamp is not None and to_timestamp is not None:
                start_date = timestamp_to_datetime(from_timestamp)
                end_date = timestamp_to_datetime(to_timestamp)
            else:
                start_date = None
                end_date = None
            summary = {
                'start_date': start_date,
                'end_date': end_date,
                'start_balance': Satoshis(start_balance),
                'end_balance': Satoshis(end_balance),
                'income': Satoshis(income),
                'expenditures': Satoshis(expenditures)
            }
            if fx and fx.is_enabled():
                unrealized = self.unrealized_gains(domain, fx.timestamp_rate, fx.ccy)
                summary['capital_gains'] = Fiat(capital_gains, fx.ccy)
                summary['fiat_income'] = Fiat(fiat_income, fx.ccy)
                summary['fiat_expenditures'] = Fiat(fiat_expenditures, fx.ccy)
                summary['unrealized_gains'] = Fiat(unrealized, fx.ccy)
                summary['start_fiat_balance'] = Fiat(fx.historical_value(start_balance, start_date), fx.ccy)
                summary['end_fiat_balance'] = Fiat(fx.historical_value(end_balance, end_date), fx.ccy)
        else:
            summary = {}
        return {
            'transactions': out,
            'summary': summary
        }

    def get_label(self, tx_hash):
        label = self.labels.get(tx_hash, '')
        if label is '':
            label = self.get_default_label(tx_hash)
        return label

    def get_default_label(self, tx_hash):
        if self.txi.get(tx_hash) == {}:
            d = self.txo.get(tx_hash, {})
            labels = []
            for addr in d.keys():
                label = self.labels.get(addr)
                if label:
                    labels.append(label)
            return ', '.join(labels)
        return ''

    def get_tx_status(self, tx_hash, height, conf, timestamp):
        from .util import format_time
        extra = []
        if conf == 0:
            tx = self.transactions.get(tx_hash)
            if not tx:
                return 2, 'unknown'
            is_final = tx and tx.is_final()
            if not is_final:
                extra.append('rbf')
            fee = self.get_wallet_delta(tx)[3]
            if fee is None:
                fee = self.tx_fees.get(tx_hash)
            if fee is not None:
                size = tx.estimated_size()
                fee_per_byte = fee / size
                extra.append('%.1f sat/b'%(fee_per_byte))
            if fee is not None and height in (TX_HEIGHT_UNCONF_PARENT, TX_HEIGHT_UNCONFIRMED) \
               and self.network and self.network.config.has_fee_mempool():
                exp_n = self.network.config.fee_to_depth(fee_per_byte)
                if exp_n:
                    extra.append('%.2f MB'%(exp_n/1000000))
            if height == TX_HEIGHT_LOCAL:
                status = 3
            elif height == TX_HEIGHT_UNCONF_PARENT:
                status = 1
            elif height == TX_HEIGHT_UNCONFIRMED:
                status = 0
            else:
                status = 2
        else:
            status = 3 + min(conf, 6)
        time_str = format_time(timestamp) if timestamp else _("unknown")
        status_str = TX_STATUS[status] if status < 4 else time_str
        if extra:
            status_str += ' [%s]'%(', '.join(extra))
        return status, status_str

    def relayfee(self):
        return relayfee(self.network)

    def dust_threshold(self):
        return dust_threshold(self.network)

    def make_unsigned_transaction(self, inputs, outputs, config, fixed_fee=None,
                                  change_addr=None, is_sweep=False):
        # check outputs
        i_max = None
        for i, o in enumerate(outputs):
            _type, data, value = o
            if _type == TYPE_ADDRESS:
                if not is_address(data):
                    raise BaseException("Invalid litecoin address:" + data)
            if value == '!':
                if i_max is not None:
                    raise BaseException("More than one output set to spend max")
                i_max = i

        # Avoid index-out-of-range with inputs[0] below
        if not inputs:
            raise NotEnoughFunds()

        if fixed_fee is None and config.fee_per_kb() is None:
            raise NoDynamicFeeEstimates()

        if not is_sweep:
            for item in inputs:
                self.add_input_info(item)

        # change address
        if change_addr:
            change_addrs = [change_addr]
        else:
            addrs = self.get_change_addresses()[-self.gap_limit_for_change:]
            if self.use_change and addrs:
                # New change addresses are created only after a few
                # confirmations.  Select the unused addresses within the
                # gap limit; if none take one at random
                change_addrs = [addr for addr in addrs if
                                self.get_num_tx(addr) == 0]
                if not change_addrs:
                    change_addrs = [random.choice(addrs)]
            else:
                # coin_chooser will set change address
                change_addrs = []

        # Fee estimator
        if fixed_fee is None:
            fee_estimator = config.estimate_fee
        elif isinstance(fixed_fee, Number):
            fee_estimator = lambda size: fixed_fee
        elif callable(fixed_fee):
            fee_estimator = fixed_fee
        else:
            raise BaseException('Invalid argument fixed_fee: %s' % fixed_fee)

        if i_max is None:
            # Let the coin chooser select the coins to spend
            max_change = self.max_change_outputs if self.multiple_change else 1
            coin_chooser = coinchooser.get_coin_chooser(config)
            tx = coin_chooser.make_tx(inputs, outputs, change_addrs[:max_change],
                                      fee_estimator, self.dust_threshold())
        else:
            # FIXME?? this might spend inputs with negative effective value...
            sendable = sum(map(lambda x:x['value'], inputs))
            _type, data, value = outputs[i_max]
            outputs[i_max] = (_type, data, 0)
            tx = Transaction.from_io(inputs, outputs[:])
            fee = fee_estimator(tx.estimated_size())
            amount = max(0, sendable - tx.output_value() - fee)
            outputs[i_max] = (_type, data, amount)
            tx = Transaction.from_io(inputs, outputs[:])

        # Sort the inputs and outputs deterministically
        tx.BIP_LI01_sort()
        # Timelock tx to current height.
        tx.locktime = self.get_local_height()
        run_hook('make_unsigned_transaction', self, tx)
        return tx

    def mktx(self, outputs, password, config, fee=None, change_addr=None, domain=None):
        coins = self.get_spendable_coins(domain, config)
        tx = self.make_unsigned_transaction(coins, outputs, config, fee, change_addr)
        self.sign_transaction(tx, password)
        return tx

    def is_frozen(self, addr):
        return addr in self.frozen_addresses

    def set_frozen_state(self, addrs, freeze):
        '''Set frozen state of the addresses to FREEZE, True or False'''
        if all(self.is_mine(addr) for addr in addrs):
            if freeze:
                self.frozen_addresses |= set(addrs)
            else:
                self.frozen_addresses -= set(addrs)
            self.storage.put('frozen_addresses', list(self.frozen_addresses))
            return True
        return False

    def prepare_for_verifier(self):
        # review transactions that are in the history
        for addr, hist in self.history.items():
            for tx_hash, tx_height in hist:
                # add it in case it was previously unconfirmed
                self.add_unverified_tx(tx_hash, tx_height)

    def start_threads(self, network):
        self.network = network
        # prepare self.unverified_tx regardless of network
        self.prepare_for_verifier()
        if self.network is not None:
            self.verifier = SPV(self.network, self)
            self.synchronizer = Synchronizer(self, network)
            network.add_jobs([self.verifier, self.synchronizer])
        else:
            self.verifier = None
            self.synchronizer = None

    def stop_threads(self):
        if self.network:
            self.network.remove_jobs([self.synchronizer, self.verifier])
            self.synchronizer.release()
            self.synchronizer = None
            self.verifier = None
            # Now no references to the syncronizer or verifier
            # remain so they will be GC-ed
            self.storage.put('stored_height', self.get_local_height())
        self.save_transactions()
        self.storage.put('verified_tx3', self.verified_tx)
        self.storage.write()

    def wait_until_synchronized(self, callback=None):
        def wait_for_wallet():
            self.set_up_to_date(False)
            while not self.is_up_to_date():
                if callback:
                    msg = "%s\n%s %d"%(
                        _("Please wait..."),
                        _("Addresses generated:"),
                        len(self.addresses(True)))
                    callback(msg)
                time.sleep(0.1)
        def wait_for_network():
            while not self.network.is_connected():
                if callback:
                    msg = "%s \n" % (_("Connecting..."))
                    callback(msg)
                time.sleep(0.1)
        # wait until we are connected, because the user
        # might have selected another server
        if self.network:
            wait_for_network()
            wait_for_wallet()
        else:
            self.synchronize()

    def can_export(self):
        return not self.is_watching_only() and hasattr(self.keystore, 'get_private_key')

    def is_used(self, address):
        h = self.history.get(address,[])
        if len(h) == 0:
            return False
        c, u, x = self.get_addr_balance(address)
        return c + u + x == 0

    def is_empty(self, address):
        c, u, x = self.get_addr_balance(address)
        return c+u+x == 0

    def address_is_old(self, address, age_limit=2):
        age = -1
        h = self.history.get(address, [])
        for tx_hash, tx_height in h:
            if tx_height <= 0:
                tx_age = 0
            else:
                tx_age = self.get_local_height() - tx_height + 1
            if tx_age > age:
                age = tx_age
        return age > age_limit

    def bump_fee(self, tx, delta):
        if tx.is_final():
            raise BaseException(_("Cannot bump fee: transaction is final"))
        inputs = copy.deepcopy(tx.inputs())
        outputs = copy.deepcopy(tx.outputs())
        for txin in inputs:
            txin['signatures'] = [None] * len(txin['signatures'])
            self.add_input_info(txin)
        # use own outputs
        s = list(filter(lambda x: self.is_mine(x[1]), outputs))
        # ... unless there is none
        if not s:
            s = outputs
            x_fee = run_hook('get_tx_extra_fee', self, tx)
            if x_fee:
                x_fee_address, x_fee_amount = x_fee
                s = filter(lambda x: x[1]!=x_fee_address, s)

        # prioritize low value outputs, to get rid of dust
        s = sorted(s, key=lambda x: x[2])
        for o in s:
            i = outputs.index(o)
            otype, address, value = o
            if value - delta >= self.dust_threshold():
                outputs[i] = otype, address, value - delta
                delta = 0
                break
            else:
                del outputs[i]
                delta -= value
                if delta > 0:
                    continue
        if delta > 0:
            raise BaseException(_('Cannot bump fee: could not find suitable outputs'))
        locktime = self.get_local_height()
        tx_new = Transaction.from_io(inputs, outputs, locktime=locktime)
        tx_new.BIP_LI01_sort()
        return tx_new

    def cpfp(self, tx, fee):
        txid = tx.txid()
        for i, o in enumerate(tx.outputs()):
            otype, address, value = o
            if otype == TYPE_ADDRESS and self.is_mine(address):
                break
        else:
            return
        coins = self.get_addr_utxo(address)
        item = coins.get(txid+':%d'%i)
        if not item:
            return
        self.add_input_info(item)
        inputs = [item]
        outputs = [(TYPE_ADDRESS, address, value - fee)]
        locktime = self.get_local_height()
        # note: no need to call tx.BIP_LI01_sort() here - single input/output
        return Transaction.from_io(inputs, outputs, locktime=locktime)

    def add_input_info(self, txin):
        address = txin['address']
        if self.is_mine(address):
            txin['type'] = self.get_txin_type(address)
            # segwit needs value to sign
            if txin.get('value') is None and Transaction.is_segwit_input(txin):
                received, spent = self.get_addr_io(address)
                item = received.get(txin['prevout_hash']+':%d'%txin['prevout_n'])
                tx_height, value, is_cb = item
                txin['value'] = value
            self.add_input_sig_info(txin, address)

    def can_sign(self, tx):
        if tx.is_complete():
            return False
        for k in self.get_keystores():
            if k.can_sign(tx):
                return True
        return False

    def get_input_tx(self, tx_hash, ignore_timeout=False):
        # First look up an input transaction in the wallet where it
        # will likely be.  If co-signing a transaction it may not have
        # all the input txs, in which case we ask the network.
        tx = self.transactions.get(tx_hash, None)
        if not tx and self.network:
            request = ('blockchain.transaction.get', [tx_hash])
            try:
                tx = Transaction(self.network.synchronous_get(request))
            except TimeoutException as e:
                self.print_error('getting input txn from network timed out for {}'.format(tx_hash))
                if not ignore_timeout:
                    raise e
        return tx

    def add_hw_info(self, tx):
        # add previous tx for hw wallets
        for txin in tx.inputs():
            tx_hash = txin['prevout_hash']
            # segwit inputs might not be needed for some hw wallets
            ignore_timeout = Transaction.is_segwit_input(txin)
            txin['prev_tx'] = self.get_input_tx(tx_hash, ignore_timeout)
        # add output info for hw wallets
        info = {}
        xpubs = self.get_master_public_keys()
        for txout in tx.outputs():
            _type, addr, amount = txout
            if self.is_change(addr):
                index = self.get_address_index(addr)
                pubkeys = self.get_public_keys(addr)
                # sort xpubs using the order of pubkeys
                sorted_pubkeys, sorted_xpubs = zip(*sorted(zip(pubkeys, xpubs)))
                info[addr] = index, sorted_xpubs, self.m if isinstance(self, Multisig_Wallet) else None
        tx.output_info = info

    def sign_transaction(self, tx, password):
        if self.is_watching_only():
            return
        # hardware wallets require extra info
        if any([(isinstance(k, Hardware_KeyStore) and k.can_sign(tx)) for k in self.get_keystores()]):
            self.add_hw_info(tx)
        # sign
        for k in self.get_keystores():
            try:
                if k.can_sign(tx):
                    k.sign_transaction(tx, password)
            except UserCancelled:
                continue

    def get_unused_addresses(self):
        # fixme: use slots from expired requests
        domain = self.get_receiving_addresses()
        return [addr for addr in domain if not self.history.get(addr)
                and addr not in self.receive_requests.keys()]

    def get_unused_address(self):
        addrs = self.get_unused_addresses()
        if addrs:
            return addrs[0]

    def get_receiving_address(self):
        # always return an address
        domain = self.get_receiving_addresses()
        if not domain:
            return
        choice = domain[0]
        for addr in domain:
            if not self.history.get(addr):
                if addr not in self.receive_requests.keys():
                    return addr
                else:
                    choice = addr
        return choice

    def get_payment_status(self, address, amount):
        local_height = self.get_local_height()
        received, sent = self.get_addr_io(address)
        l = []
        for txo, x in received.items():
            h, v, is_cb = x
            txid, n = txo.split(':')
            info = self.verified_tx.get(txid)
            if info:
                tx_height, timestamp, pos = info
                conf = local_height - tx_height
            else:
                conf = 0
            l.append((conf, v))
        vsum = 0
        for conf, v in reversed(sorted(l)):
            vsum += v
            if vsum >= amount:
                return True, conf
        return False, None

    def get_payment_request(self, addr, config):
        r = self.receive_requests.get(addr)
        if not r:
            return
        out = copy.copy(r)
        out['URI'] = 'litecoin:' + addr + '?amount=' + format_satoshis(out.get('amount'))
        status, conf = self.get_request_status(addr)
        out['status'] = status
        if conf is not None:
            out['confirmations'] = conf
        # check if bip70 file exists
        rdir = config.get('requests_dir')
        if rdir:
            key = out.get('id', addr)
            path = os.path.join(rdir, 'req', key[0], key[1], key)
            if os.path.exists(path):
                baseurl = 'file://' + rdir
                rewrite = config.get('url_rewrite')
                if rewrite:
                    baseurl = baseurl.replace(*rewrite)
                out['request_url'] = os.path.join(baseurl, 'req', key[0], key[1], key, key)
                out['URI'] += '&r=' + out['request_url']
                out['index_url'] = os.path.join(baseurl, 'index.html') + '?id=' + key
                websocket_server_announce = config.get('websocket_server_announce')
                if websocket_server_announce:
                    out['websocket_server'] = websocket_server_announce
                else:
                    out['websocket_server'] = config.get('websocket_server', 'localhost')
                websocket_port_announce = config.get('websocket_port_announce')
                if websocket_port_announce:
                    out['websocket_port'] = websocket_port_announce
                else:
                    out['websocket_port'] = config.get('websocket_port', 9999)
        return out

    def get_request_status(self, key):
        r = self.receive_requests.get(key)
        if r is None:
            return PR_UNKNOWN
        address = r['address']
        amount = r.get('amount')
        timestamp = r.get('time', 0)
        if timestamp and type(timestamp) != int:
            timestamp = 0
        expiration = r.get('exp')
        if expiration and type(expiration) != int:
            expiration = 0
        conf = None
        if amount:
            if self.up_to_date:
                paid, conf = self.get_payment_status(address, amount)
                status = PR_PAID if paid else PR_UNPAID
                if status == PR_UNPAID and expiration is not None and time.time() > timestamp + expiration:
                    status = PR_EXPIRED
            else:
                status = PR_UNKNOWN
        else:
            status = PR_UNKNOWN
        return status, conf

    def make_payment_request(self, addr, amount, message, expiration):
        timestamp = int(time.time())
        _id = bh2u(Hash(addr + "%d"%timestamp))[0:10]
        r = {'time':timestamp, 'amount':amount, 'exp':expiration, 'address':addr, 'memo':message, 'id':_id}
        return r

    def sign_payment_request(self, key, alias, alias_addr, password):
        req = self.receive_requests.get(key)
        alias_privkey = self.export_private_key(alias_addr, password)[0]
        pr = paymentrequest.make_unsigned_request(req)
        paymentrequest.sign_request_with_alias(pr, alias, alias_privkey)
        req['name'] = pr.pki_data
        req['sig'] = bh2u(pr.signature)
        self.receive_requests[key] = req
        self.storage.put('payment_requests', self.receive_requests)

    def add_payment_request(self, req, config):
        addr = req['address']
        amount = req.get('amount')
        message = req.get('memo')
        self.receive_requests[addr] = req
        self.storage.put('payment_requests', self.receive_requests)
        self.set_label(addr, message) # should be a default label

        rdir = config.get('requests_dir')
        if rdir and amount is not None:
            key = req.get('id', addr)
            pr = paymentrequest.make_request(config, req)
            path = os.path.join(rdir, 'req', key[0], key[1], key)
            if not os.path.exists(path):
                try:
                    os.makedirs(path)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
            with open(os.path.join(path, key), 'wb') as f:
                f.write(pr.SerializeToString())
            # reload
            req = self.get_payment_request(addr, config)
            with open(os.path.join(path, key + '.json'), 'w') as f:
                f.write(json.dumps(req))
        return req

    def remove_payment_request(self, addr, config):
        if addr not in self.receive_requests:
            return False
        r = self.receive_requests.pop(addr)
        rdir = config.get('requests_dir')
        if rdir:
            key = r.get('id', addr)
            for s in ['.json', '']:
                n = os.path.join(rdir, 'req', key[0], key[1], key, key + s)
                if os.path.exists(n):
                    os.unlink(n)
        self.storage.put('payment_requests', self.receive_requests)
        return True

    def get_sorted_requests(self, config):
        def f(x):
            try:
                addr = x.get('address')
                return self.get_address_index(addr) or addr
            except:
                return addr
        return sorted(map(lambda x: self.get_payment_request(x, config), self.receive_requests.keys()), key=f)

    def get_fingerprint(self):
        raise NotImplementedError()

    def can_import_privkey(self):
        return False

    def can_import_address(self):
        return False

    def can_delete_address(self):
        return False

    def add_address(self, address):
        if address not in self.history:
            self.history[address] = []
        if self.synchronizer:
            self.synchronizer.add(address)

    def has_password(self):
        return self.has_keystore_encryption() or self.has_storage_encryption()

    def can_have_keystore_encryption(self):
        return self.keystore and self.keystore.may_have_password()

    def get_available_storage_encryption_version(self):
        """Returns the type of storage encryption offered to the user.

        A wallet file (storage) is either encrypted with this version
        or is stored in plaintext.
        """
        if isinstance(self.keystore, Hardware_KeyStore):
            return STO_EV_XPUB_PW
        else:
            return STO_EV_USER_PW

    def has_keystore_encryption(self):
        """Returns whether encryption is enabled for the keystore.

        If True, e.g. signing a transaction will require a password.
        """
        if self.can_have_keystore_encryption():
            return self.storage.get('use_encryption', False)
        return False

    def has_storage_encryption(self):
        """Returns whether encryption is enabled for the wallet file on disk."""
        return self.storage.is_encrypted()

    @classmethod
    def may_have_password(cls):
        return True

    def check_password(self, password):
        if self.has_keystore_encryption():
            self.keystore.check_password(password)
        self.storage.check_password(password)

    def update_password(self, old_pw, new_pw, encrypt_storage=False):
        if old_pw is None and self.has_password():
            raise InvalidPassword()
        self.check_password(old_pw)

        if encrypt_storage:
            enc_version = self.get_available_storage_encryption_version()
        else:
            enc_version = STO_EV_PLAINTEXT
        self.storage.set_password(new_pw, enc_version)

        # note: Encrypting storage with a hw device is currently only
        #       allowed for non-multisig wallets. Further,
        #       Hardware_KeyStore.may_have_password() == False.
        #       If these were not the case,
        #       extra care would need to be taken when encrypting keystores.
        self._update_password_for_keystore(old_pw, new_pw)
        encrypt_keystore = self.can_have_keystore_encryption()
        self.storage.set_keystore_encryption(bool(new_pw) and encrypt_keystore)

        self.storage.write()

    def sign_message(self, address, message, password):
        index = self.get_address_index(address)
        return self.keystore.sign_message(index, message, password)

    def decrypt_message(self, pubkey, message, password):
        addr = self.pubkeys_to_address(pubkey)
        index = self.get_address_index(addr)
        return self.keystore.decrypt_message(index, message, password)

    def get_depending_transactions(self, tx_hash):
        """Returns all (grand-)children of tx_hash in this wallet."""
        children = set()
        for other_hash, tx in self.transactions.items():
            for input in (tx.inputs()):
                if input["prevout_hash"] == tx_hash:
                    children.add(other_hash)
                    children |= self.get_depending_transactions(other_hash)
        return children

    def txin_value(self, txin):
        txid = txin['prevout_hash']
        prev_n = txin['prevout_n']
        for address, d in self.txo[txid].items():
            for n, v, cb in d:
                if n == prev_n:
                    return v
        raise BaseException('unknown txin value')

    def price_at_timestamp(self, txid, price_func):
        height, conf, timestamp = self.get_tx_height(txid)
        return price_func(timestamp if timestamp else time.time())

    def unrealized_gains(self, domain, price_func, ccy):
        coins = self.get_utxos(domain)
        now = time.time()
        p = price_func(now)
        ap = sum(self.coin_price(coin['prevout_hash'], price_func, ccy, self.txin_value(coin)) for coin in coins)
        lp = sum([coin['value'] for coin in coins]) * p / Decimal(COIN)
        return lp - ap

    def average_price(self, txid, price_func, ccy):
        """ Average acquisition price of the inputs of a transaction """
        input_value = 0
        total_price = 0
        for addr, d in self.txi.get(txid, {}).items():
            for ser, v in d:
                input_value += v
                total_price += self.coin_price(ser.split(':')[0], price_func, ccy, v)
        return total_price / (input_value/Decimal(COIN))

    def coin_price(self, txid, price_func, ccy, txin_value):
        """
        Acquisition price of a coin.
        This assumes that either all inputs are mine, or no input is mine.
        """
        cache_key = "{}:{}:{}".format(str(txid), str(ccy), str(txin_value))
        result = self.coin_price_cache.get(cache_key, None)
        if result is not None:
            return result
        if self.txi.get(txid, {}) != {}:
            result = self.average_price(txid, price_func, ccy) * txin_value/Decimal(COIN)
            self.coin_price_cache[cache_key] = result
            return result
        else:
            fiat_value = self.get_fiat_value(txid, ccy)
            if fiat_value is not None:
                return fiat_value
            else:
                p = self.price_at_timestamp(txid, price_func)
                return p * txin_value/Decimal(COIN)


class Simple_Wallet(Abstract_Wallet):
    # wallet with a single keystore

    def get_keystore(self):
        return self.keystore

    def get_keystores(self):
        return [self.keystore]

    def is_watching_only(self):
        return self.keystore.is_watching_only()

    def _update_password_for_keystore(self, old_pw, new_pw):
        if self.keystore and self.keystore.may_have_password():
            self.keystore.update_password(old_pw, new_pw)
            self.save_keystore()

    def save_keystore(self):
        self.storage.put('keystore', self.keystore.dump())


class Imported_Wallet(Simple_Wallet):
    # wallet made of imported addresses

    wallet_type = 'imported'
    txin_type = 'address'

    def __init__(self, storage):
        Abstract_Wallet.__init__(self, storage)

    def is_watching_only(self):
        return self.keystore is None

    def get_keystores(self):
        return [self.keystore] if self.keystore else []

    def can_import_privkey(self):
        return bool(self.keystore)

    def load_keystore(self):
        self.keystore = load_keystore(self.storage, 'keystore') if self.storage.get('keystore') else None

    def save_keystore(self):
        self.storage.put('keystore', self.keystore.dump())

    def load_addresses(self):
        self.addresses = self.storage.get('addresses', {})
        # fixme: a reference to addresses is needed
        if self.keystore:
            self.keystore.addresses = self.addresses

    def save_addresses(self):
        self.storage.put('addresses', self.addresses)

    def can_import_address(self):
        return self.is_watching_only()

    def can_delete_address(self):
        return True

    def has_seed(self):
        return False

    def is_deterministic(self):
        return False

    def is_change(self, address):
        return False

    def get_master_public_keys(self):
        return []

    def is_beyond_limit(self, address):
        return False

    def is_mine(self, address):
        return address in self.addresses

    def get_fingerprint(self):
        return ''

    def get_addresses(self, include_change=False):
        return sorted(self.addresses.keys())

    def get_receiving_addresses(self):
        return self.get_addresses()

    def get_change_addresses(self):
        return []

    def import_address(self, address):
        if not bitcoin.is_address(address):
            return ''
        if address in self.addresses:
            return ''
        self.addresses[address] = {}
        self.storage.put('addresses', self.addresses)
        self.storage.write()
        self.add_address(address)
        return address

    def delete_address(self, address):
        if address not in self.addresses:
            return

        transactions_to_remove = set()  # only referred to by this address
        transactions_new = set()  # txs that are not only referred to by address
        with self.lock:
            for addr, details in self.history.items():
                if addr == address:
                    for tx_hash, height in details:
                        transactions_to_remove.add(tx_hash)
                else:
                    for tx_hash, height in details:
                        transactions_new.add(tx_hash)
            transactions_to_remove -= transactions_new
            self.history.pop(address, None)

            for tx_hash in transactions_to_remove:
                self.remove_transaction(tx_hash)
                self.tx_fees.pop(tx_hash, None)
                self.verified_tx.pop(tx_hash, None)
                self.unverified_tx.pop(tx_hash, None)
                self.transactions.pop(tx_hash, None)
                # FIXME: what about pruned_txo?

        self.storage.put('verified_tx3', self.verified_tx)
        self.save_transactions()

        self.set_label(address, None)
        self.remove_payment_request(address, {})
        self.set_frozen_state([address], False)

        pubkey = self.get_public_key(address)
        self.addresses.pop(address)
        if pubkey:
            self.keystore.delete_imported_key(pubkey)
            self.save_keystore()
        self.storage.put('addresses', self.addresses)

        self.storage.write()

    def get_address_index(self, address):
        return self.get_public_key(address)

    def get_public_key(self, address):
        return self.addresses[address].get('pubkey')

    def import_private_key(self, sec, pw, redeem_script=None):
        try:
            txin_type, pubkey = self.keystore.import_privkey(sec, pw)
        except Exception:
            neutered_privkey = str(sec)[:3] + '..' + str(sec)[-2:]
            raise BaseException('Invalid private key', neutered_privkey)
        if txin_type in ['p2pkh', 'p2wpkh', 'p2wpkh-p2sh']:
            if redeem_script is not None:
                raise BaseException('Cannot use redeem script with', txin_type)
            addr = bitcoin.pubkey_to_address(txin_type, pubkey)
        elif txin_type in ['p2sh', 'p2wsh', 'p2wsh-p2sh']:
            if redeem_script is None:
                raise BaseException('Redeem script required for', txin_type)
            addr = bitcoin.redeem_script_to_address(txin_type, redeem_script)
        else:
            raise NotImplementedError(txin_type)
        self.addresses[addr] = {'type':txin_type, 'pubkey':pubkey, 'redeem_script':redeem_script}
        self.save_keystore()
        self.save_addresses()
        self.storage.write()
        self.add_address(addr)
        return addr

    def get_redeem_script(self, address):
        d = self.addresses[address]
        redeem_script = d['redeem_script']
        return redeem_script

    def get_txin_type(self, address):
        return self.addresses[address].get('type', 'address')

    def add_input_sig_info(self, txin, address):
        if self.is_watching_only():
            x_pubkey = 'fd' + address_to_script(address)
            txin['x_pubkeys'] = [x_pubkey]
            txin['signatures'] = [None]
            return
        if txin['type'] in ['p2pkh', 'p2wpkh', 'p2wpkh-p2sh']:
            pubkey = self.addresses[address]['pubkey']
            txin['num_sig'] = 1
            txin['x_pubkeys'] = [pubkey]
            txin['signatures'] = [None]
        else:
            redeem_script = self.addresses[address]['redeem_script']
            num_sig = 2
            num_keys = 3
            txin['num_sig'] = num_sig
            txin['redeem_script'] = redeem_script
            txin['signatures'] = [None] * num_keys

    def pubkeys_to_address(self, pubkey):
        for addr, v in self.addresses.items():
            if v.get('pubkey') == pubkey:
                return addr

class Deterministic_Wallet(Abstract_Wallet):

    def __init__(self, storage):
        Abstract_Wallet.__init__(self, storage)
        self.gap_limit = storage.get('gap_limit', 20)

    def has_seed(self):
        return self.keystore.has_seed()

    def get_receiving_addresses(self):
        return self.receiving_addresses

    def get_change_addresses(self):
        return self.change_addresses

    def get_seed(self, password):
        return self.keystore.get_seed(password)

    def add_seed(self, seed, pw):
        self.keystore.add_seed(seed, pw)

    def change_gap_limit(self, value):
        '''This method is not called in the code, it is kept for console use'''
        if value >= self.gap_limit:
            self.gap_limit = value
            self.storage.put('gap_limit', self.gap_limit)
            return True
        elif value >= self.min_acceptable_gap():
            addresses = self.get_receiving_addresses()
            k = self.num_unused_trailing_addresses(addresses)
            n = len(addresses) - k + value
            self.receiving_addresses = self.receiving_addresses[0:n]
            self.gap_limit = value
            self.storage.put('gap_limit', self.gap_limit)
            self.save_addresses()
            return True
        else:
            return False

    def num_unused_trailing_addresses(self, addresses):
        k = 0
        for a in addresses[::-1]:
            if self.history.get(a):break
            k = k + 1
        return k

    def min_acceptable_gap(self):
        # fixme: this assumes wallet is synchronized
        n = 0
        nmax = 0
        addresses = self.get_receiving_addresses()
        k = self.num_unused_trailing_addresses(addresses)
        for a in addresses[0:-k]:
            if self.history.get(a):
                n = 0
            else:
                n += 1
                if n > nmax: nmax = n
        return nmax + 1

    def load_addresses(self):
        super().load_addresses()
        self._addr_to_addr_index = {}  # key: address, value: (is_change, index)
        for i, addr in enumerate(self.receiving_addresses):
            self._addr_to_addr_index[addr] = (False, i)
        for i, addr in enumerate(self.change_addresses):
            self._addr_to_addr_index[addr] = (True, i)

    def create_new_address(self, for_change=False):
        assert type(for_change) is bool
        with self.lock:
            addr_list = self.change_addresses if for_change else self.receiving_addresses
            n = len(addr_list)
            x = self.derive_pubkeys(for_change, n)
            address = self.pubkeys_to_address(x)
            addr_list.append(address)
            self._addr_to_addr_index[address] = (for_change, n)
            self.save_addresses()
            self.add_address(address)
            return address

    def synchronize_sequence(self, for_change):
        limit = self.gap_limit_for_change if for_change else self.gap_limit
        while True:
            addresses = self.get_change_addresses() if for_change else self.get_receiving_addresses()
            if len(addresses) < limit:
                self.create_new_address(for_change)
                continue
            if list(map(lambda a: self.address_is_old(a), addresses[-limit:] )) == limit*[False]:
                break
            else:
                self.create_new_address(for_change)

    def synchronize(self):
        with self.lock:
            self.synchronize_sequence(False)
            self.synchronize_sequence(True)

    def is_beyond_limit(self, address):
        is_change, i = self.get_address_index(address)
        addr_list = self.get_change_addresses() if is_change else self.get_receiving_addresses()
        limit = self.gap_limit_for_change if is_change else self.gap_limit
        if i < limit:
            return False
        prev_addresses = addr_list[max(0, i - limit):max(0, i)]
        for addr in prev_addresses:
            if self.history.get(addr):
                return False
        return True

    def is_mine(self, address):
        return address in self._addr_to_addr_index

    def get_address_index(self, address):
        return self._addr_to_addr_index[address]

    def get_master_public_keys(self):
        return [self.get_master_public_key()]

    def get_fingerprint(self):
        return self.get_master_public_key()

    def get_txin_type(self, address):
        return self.txin_type


class Simple_Deterministic_Wallet(Simple_Wallet, Deterministic_Wallet):

    """ Deterministic Wallet with a single pubkey per address """

    def __init__(self, storage):
        Deterministic_Wallet.__init__(self, storage)

    def get_public_key(self, address):
        sequence = self.get_address_index(address)
        pubkey = self.get_pubkey(*sequence)
        return pubkey

    def load_keystore(self):
        self.keystore = load_keystore(self.storage, 'keystore')
        try:
            xtype = bitcoin.xpub_type(self.keystore.xpub)
        except:
            xtype = 'standard'
        self.txin_type = 'p2pkh' if xtype == 'standard' else xtype

    def get_pubkey(self, c, i):
        return self.derive_pubkeys(c, i)

    def add_input_sig_info(self, txin, address):
        derivation = self.get_address_index(address)
        x_pubkey = self.keystore.get_xpubkey(*derivation)
        txin['x_pubkeys'] = [x_pubkey]
        txin['signatures'] = [None]
        txin['num_sig'] = 1

    def get_master_public_key(self):
        return self.keystore.get_master_public_key()

    def derive_pubkeys(self, c, i):
        return self.keystore.derive_pubkey(c, i)






class Standard_Wallet(Simple_Deterministic_Wallet):
    wallet_type = 'standard'

    def pubkeys_to_address(self, pubkey):
        return bitcoin.pubkey_to_address(self.txin_type, pubkey)


class Multisig_Wallet(Deterministic_Wallet):
    # generic m of n
    gap_limit = 20

    def __init__(self, storage):
        self.wallet_type = storage.get('wallet_type')
        self.m, self.n = multisig_type(self.wallet_type)
        Deterministic_Wallet.__init__(self, storage)

    def get_pubkeys(self, c, i):
        return self.derive_pubkeys(c, i)

    def get_public_keys(self, address):
        sequence = self.get_address_index(address)
        return self.get_pubkeys(*sequence)

    def pubkeys_to_address(self, pubkeys):
        redeem_script = self.pubkeys_to_redeem_script(pubkeys)
        return bitcoin.redeem_script_to_address(self.txin_type, redeem_script)

    def pubkeys_to_redeem_script(self, pubkeys):
        return transaction.multisig_script(sorted(pubkeys), self.m)

    def get_redeem_script(self, address):
        pubkeys = self.get_public_keys(address)
        redeem_script = self.pubkeys_to_redeem_script(pubkeys)
        return redeem_script

    def derive_pubkeys(self, c, i):
        return [k.derive_pubkey(c, i) for k in self.get_keystores()]

    def load_keystore(self):
        self.keystores = {}
        for i in range(self.n):
            name = 'x%d/'%(i+1)
            self.keystores[name] = load_keystore(self.storage, name)
        self.keystore = self.keystores['x1/']
        xtype = bitcoin.xpub_type(self.keystore.xpub)
        self.txin_type = 'p2sh' if xtype == 'standard' else xtype

    def save_keystore(self):
        for name, k in self.keystores.items():
            self.storage.put(name, k.dump())

    def get_keystore(self):
        return self.keystores.get('x1/')

    def get_keystores(self):
        return [self.keystores[i] for i in sorted(self.keystores.keys())]

    def can_have_keystore_encryption(self):
        return any([k.may_have_password() for k in self.get_keystores()])

    def _update_password_for_keystore(self, old_pw, new_pw):
        for name, keystore in self.keystores.items():
            if keystore.may_have_password():
                keystore.update_password(old_pw, new_pw)
                self.storage.put(name, keystore.dump())

    def check_password(self, password):
        for name, keystore in self.keystores.items():
            if keystore.may_have_password():
                keystore.check_password(password)
        self.storage.check_password(password)

    def get_available_storage_encryption_version(self):
        # multisig wallets are not offered hw device encryption
        return STO_EV_USER_PW

    def has_seed(self):
        return self.keystore.has_seed()

    def is_watching_only(self):
        return not any([not k.is_watching_only() for k in self.get_keystores()])

    def get_master_public_key(self):
        return self.keystore.get_master_public_key()

    def get_master_public_keys(self):
        return [k.get_master_public_key() for k in self.get_keystores()]

    def get_fingerprint(self):
        return ''.join(sorted(self.get_master_public_keys()))

    def add_input_sig_info(self, txin, address):
        # x_pubkeys are not sorted here because it would be too slow
        # they are sorted in transaction.get_sorted_pubkeys
        # pubkeys is set to None to signal that x_pubkeys are unsorted
        derivation = self.get_address_index(address)
        txin['x_pubkeys'] = [k.get_xpubkey(*derivation) for k in self.get_keystores()]
        txin['pubkeys'] = None
        # we need n place holders
        txin['signatures'] = [None] * self.n
        txin['num_sig'] = self.m


wallet_types = ['standard', 'multisig', 'imported']

def register_wallet_type(category):
    wallet_types.append(category)

wallet_constructors = {
    'standard': Standard_Wallet,
    'old': Standard_Wallet,
    'xpub': Standard_Wallet,
    'imported': Imported_Wallet
}

def register_constructor(wallet_type, constructor):
    wallet_constructors[wallet_type] = constructor

# former WalletFactory
class Wallet(object):
    """The main wallet "entry point".
    This class is actually a factory that will return a wallet of the correct
    type when passed a WalletStorage instance."""

    def __new__(self, storage):
        wallet_type = storage.get('wallet_type')
        WalletClass = Wallet.wallet_class(wallet_type)
        wallet = WalletClass(storage)
        # Convert hardware wallets restored with older versions of
        # Electrum to BIP44 wallets.  A hardware wallet does not have
        # a seed and plugins do not need to handle having one.
        rwc = getattr(wallet, 'restore_wallet_class', None)
        if rwc and storage.get('seed', ''):
            storage.print_error("converting wallet type to " + rwc.wallet_type)
            storage.put('wallet_type', rwc.wallet_type)
            wallet = rwc(storage)
        return wallet

    @staticmethod
    def wallet_class(wallet_type):
        if multisig_type(wallet_type):
            return Multisig_Wallet
        if wallet_type in wallet_constructors:
            return wallet_constructors[wallet_type]
        raise RuntimeError("Unknown wallet type: " + wallet_type)

