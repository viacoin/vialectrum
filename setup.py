#!/usr/bin/env python3

# python setup.py sdist --format=zip,gztar

from setuptools import setup
import os
import sys
import platform
import imp
import argparse

with open('requirements-hw.txt') as f:
    requirements_hw = f.read().splitlines()

version = imp.load_source('version', 'lib/version.py')

if sys.version_info[:3] < (3, 4, 0):
    sys.exit("Error: Electrum requires Python version >= 3.4.0...")

data_files = ['requirements-hw.txt']

if platform.system() in ['Linux', 'FreeBSD', 'DragonFly']:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root=', dest='root_path', metavar='dir', default='/')
    opts, _ = parser.parse_known_args(sys.argv[1:])
    usr_share = os.path.join(sys.prefix, "share")
    if not os.access(opts.root_path + usr_share, os.W_OK) and \
       not os.access(opts.root_path, os.W_OK):
        if 'XDG_DATA_HOME' in os.environ.keys():
            usr_share = os.environ['XDG_DATA_HOME']
        else:
            usr_share = os.path.expanduser('~/.local/share')
    data_files += [
        (os.path.join(usr_share, 'applications/'), ['vialectrum.desktop']),
        (os.path.join(usr_share, 'pixmaps/'), ['icons/vialectrum.png'])
    ]

setup(
    name="Vialectrum",
    version=version.ELECTRUM_VERSION,
    install_requires=[
        'pyaes>=0.1a1',
        'ecdsa>=0.9',
        'pbkdf2',
        'requests',
        'qrcode',
        'scrypt>=0.6.0',
        'protobuf',
        'dnspython',
        'jsonrpclib-pelix',
        'PySocks>=1.6.6',
    ],
    extras_require={
        'hardware': requirements_hw,
    },
    packages=[
        'vialectrum',
        'vialectrum_gui',
        'vialectrum_gui.qt',
        'vialectrum_plugins',
        'vialectrum_plugins.audio_modem',
        'vialectrum_plugins.cosigner_pool',
        'vialectrum_plugins.email_requests',
        'vialectrum_plugins.hw_wallet',
        'vialectrum_plugins.keepkey',
        'vialectrum_plugins.labels',
        'vialectrum_plugins.ledger',
        'vialectrum_plugins.trezor',
        'vialectrum_plugins.digitalbitbox',
        'vialectrum_plugins.virtualkeyboard',
    ],
    package_dir={
        'vialectrum': 'lib',
        'vialectrum_gui': 'gui',
        'vialectrum_plugins': 'plugins',
    },
    package_data={
        'vialectrum': [
            'servers.json',
            'servers_testnet.json',
            'currencies.json',
            'checkpoints.json',
            'checkpoints_testnet.json',
            'www/index.html',
            'wordlist/*.txt',
            'locale/*/LC_MESSAGES/electrum.mo',
        ]
    },
    scripts=['vialectrum'],
    data_files=data_files,
    description="Lightweight Viacoin Wallet",
    author="Thomas Voegtlin",
    author_email="thomasv@electrum.org",
    license="MIT Licence",
    url="http://vialectrum.org",
    long_description="""Lightweight Viacoin Wallet"""
)
