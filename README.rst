Vialectrum - Lightweight Viacoin client
==========================================

::

  Licence: MIT Licence
  Original Author: Thomas Voegtlin & Pooler
  Port Maintainer: Romano
  Language: Python
  Homepage: https://viacoin.org/






Getting started
===============

Vialectrum is a pure python application. If you want to use the
Qt interface, install the Qt dependencies::

    sudo apt-get install python3-pyqt5

If you downloaded the official package (tar.gz), you can run
Vialectrum from its root directory, without installing it on your
system; all the python dependencies are included in the 'packages'
directory. To run Vialectrum from its root directory, just do::

    ./vialectrum

You can also install Vialectrum on your system, by running this command::

    sudo apt-get install python3-setuptools
    python3 setup.py install

This will download and install the Python dependencies used by
Vialectrum, instead of using the 'packages' directory.

If you cloned the git repository, you need to compile extra files
before you can run Vialectrum. Read the next section, "Development
Version".



Development version
===================

Check out the code from Github::

    git clone git://github.com/vialectrum/vialectrum.git
    cd vialectrum

Run install (this should install dependencies)::

    python3 setup.py install

Compile the icons file for Qt::

    sudo apt-get install pyqt5-dev-tools
    pyrcc5 icons.qrc -o gui/qt/icons_rc.py

Compile the protobuf description file::

    sudo apt-get install protobuf-compiler
    protoc --proto_path=lib/ --python_out=lib/ lib/paymentrequest.proto

Create translations (optional)::

    sudo apt-get install python-requests gettext
    ./contrib/make_locale




Creating Binaries
=================


To create binaries, create the 'packages' directory::

    ./contrib/make_packages

This directory contains the python dependencies used by Electrum.

Mac OS X / macOS
--------

::

    # On MacPorts installs: 
    sudo python3 setup.py py2app
    
    # On Homebrew installs: 
    ARCHFLAGS="-arch i386 -arch x86_64" sudo python3 setup.py py2app --includes sip
    
    sudo hdiutil create -fs HFS+ -volname "Vialectrum" -srcfolder dist/Vialectrum.app dist/electrum-via-VERSION-macosx.dmg

Windows
-------

See `contrib/build-wine/README` file.


Android
-------

See `gui/kivy/Readme.txt` file.