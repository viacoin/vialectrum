#!/bin/bash

# Please update these carefully, some versions won't work under Wine
NSIS_FILENAME=nsis-3.02.1-setup.exe
NSIS_URL=https://prdownloads.sourceforge.net/nsis/$NSIS_FILENAME?download
NSIS_SHA256=736c9062a02e297e335f82252e648a883171c98e0d5120439f538c81d429552e

ZBAR_FILENAME=zbarw-20121031-setup.exe
ZBAR_URL=https://sourceforge.net/projects/zbarw/files/$ZBAR_FILENAME/download
ZBAR_SHA256=177e32b272fa76528a3af486b74e9cb356707be1c5ace4ed3fcee9723e2c2c02

LIBUSB_FILENAME=libusb-1.0.21.7z
LIBUSB_URL=https://prdownloads.sourceforge.net/project/libusb/libusb-1.0/libusb-1.0.21/$LIBUSB_FILENAME?download
LIBUSB_SHA256=acdde63a40b1477898aee6153f9d91d1a2e8a5d93f832ca8ab876498f3a6d2b8

PYTHON_VERSION=3.5.4

## These settings probably don't need change
export WINEPREFIX=/opt/wine64
#export WINEARCH='win32'

PYHOME=c:/python$PYTHON_VERSION
PYTHON="wine $PYHOME/python.exe -OO -B"


# based on https://superuser.com/questions/497940/script-to-verify-a-signature-with-gpg
verify_signature() {
    local file=$1 keyring=$2 out=
    if out=$(gpg --no-default-keyring --keyring "$keyring" --status-fd 1 --verify "$file" 2>/dev/null) &&
       echo "$out" | grep -qs "^\[GNUPG:\] VALIDSIG "; then
        return 0
    else
        echo "$out" >&2
        exit 1
    fi
}

verify_hash() {
    local file=$1 expected_hash=$2
    actual_hash=$(sha256sum $file | awk '{print $1}')
    if [ "$actual_hash" == "$expected_hash" ]; then
        return 0
    else
        echo "$file $actual_hash (unexpected hash)" >&2
        rm "$file"
        exit 1
    fi
}

download_if_not_exist() {
    local file_name=$1 url=$2
    if [ ! -e $file_name ] ; then
        wget -O $PWD/$file_name "$url"
    fi
}

# Let's begin!
here=$(dirname $(readlink -e $0))
set -e

# Clean up Wine environment
echo "Cleaning $WINEPREFIX"
rm -rf $WINEPREFIX
echo "done"

wine 'wineboot'

mkdir -p /tmp/electrum-ltc-build

cd /tmp/electrum-ltc-build

# Install Python
# note: you might need "sudo apt-get install dirmngr" for the following
# keys from https://www.python.org/downloads/#pubkeys
KEYLIST_PYTHON_DEV="531F072D39700991925FED0C0EDDC5F26A45C816 26DEA9D4613391EF3E25C9FF0A5B101836580288 CBC547978A3964D14B9AB36A6AF053F07D9DC8D2 C01E1CAD5EA2C4F0B8E3571504C367C218ADD4FF 12EF3DC38047DA382D18A5B999CDEA9DA4135B38 8417157EDBE73D9EAC1E539B126EB563A74B06BF DBBF2EEBF925FAADCF1F3FFFD9866941EA5BBD71 2BA0DB82515BBB9EFFAC71C5C9BE28DEE6DF025C 0D96DF4D4110E5C43FBFB17F2D347EA6AA65421D C9B104B3DD3AA72D7CCB1066FB9921286F5E1540 97FC712E4C024BBEA48A61ED3A5CA953F73C700D 7ED10B6531D7C8E1BC296021FC624643487034E5"
KEYRING_PYTHON_DEV="keyring-electrum-build-python-dev.gpg"
KEYSERVER_PYTHON_DEV="hkp://keys.gnupg.net"
gpg --no-default-keyring --keyring $KEYRING_PYTHON_DEV --keyserver $KEYSERVER_PYTHON_DEV --recv-keys $KEYLIST_PYTHON_DEV
for msifile in core dev exe lib pip tools; do
    echo "Installing $msifile..."
    wget -nc "https://www.python.org/ftp/python/$PYTHON_VERSION/win32/${msifile}.msi"
    wget -nc "https://www.python.org/ftp/python/$PYTHON_VERSION/win32/${msifile}.msi.asc"
    verify_signature "${msifile}.msi.asc" $KEYRING_PYTHON_DEV
    wine msiexec /i "${msifile}.msi" /qb TARGETDIR=C:/python$PYTHON_VERSION
done

# upgrade pip
$PYTHON -m pip install pip --upgrade

# Install pywin32-ctypes (needed by pyinstaller)
$PYTHON -m pip install pywin32-ctypes==0.1.2

# install PySocks
$PYTHON -m pip install win_inet_pton==1.0.1

$PYTHON -m pip install -r $here/../deterministic-build/requirements-binaries.txt

# Install PyInstaller
$PYTHON -m pip install https://github.com/ecdsa/pyinstaller/archive/fix_2952.zip

# Install ZBar
download_if_not_exist $ZBAR_FILENAME "$ZBAR_URL"
verify_hash $ZBAR_FILENAME "$ZBAR_SHA256"
wine "$PWD/$ZBAR_FILENAME" /S

# Upgrade setuptools (so Electrum can be installed later)
$PYTHON -m pip install setuptools --upgrade

# Install NSIS installer
download_if_not_exist $NSIS_FILENAME "$NSIS_URL"
verify_hash $NSIS_FILENAME "$NSIS_SHA256"
wine "$PWD/$NSIS_FILENAME" /S

download_if_not_exist $LIBUSB_FILENAME "$LIBUSB_URL"
verify_hash $LIBUSB_FILENAME "$LIBUSB_SHA256"
7z x -olibusb $LIBUSB_FILENAME -aos

cp libusb/MS32/dll/libusb-1.0.dll $WINEPREFIX/drive_c/python$PYTHON_VERSION/

# Install UPX
#wget -O upx.zip "https://downloads.sourceforge.net/project/upx/upx/3.08/upx308w.zip"
#unzip -o upx.zip
#cp upx*/upx.exe .

# add dlls needed for pyinstaller:
cp $WINEPREFIX/drive_c/python$PYTHON_VERSION/Lib/site-packages/PyQt5/Qt/bin/* $WINEPREFIX/drive_c/python$PYTHON_VERSION/

echo "Wine is configured."
