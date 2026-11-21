"""Build-time: fetch pristine sqlglot at the pinned commit into /opt/sqlglot (GitHub archive)."""
import io
import os
import shutil
import sys
import tarfile
import urllib.request

sha = sys.argv[1]
dest = sys.argv[2]
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
tmp = dest + "._x"
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp)
tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp, filter="data")
top = os.path.join(tmp, os.listdir(tmp)[0])
shutil.rmtree(dest, ignore_errors=True)
shutil.move(top, dest)
shutil.rmtree(tmp)
assert os.path.isfile(os.path.join(dest, "sqlglot", "parser.py")), "sqlglot source missing"
print("fetched", sha, "->", dest)
