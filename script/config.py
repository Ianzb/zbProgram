import os
import sys

import zbToolLib as zb

NAME = "zbProgram"
LOG_INDEX = "exe版"
IS_SINGLE_FILE = False
IS_SETUP = True
EXTRA_FILES = []
EXTRA_LIBS = ["zbWidgetLib", "xml"]

ROOT = os.path.abspath((zb.getFileDir(zb.getFileDir(sys.argv[0]))))
CODE_PATH = zb.joinPath(ROOT, NAME)
PROGRAM_PY = zb.joinPath(CODE_PATH, "app", "program", "program.py")
MAIN_PYW = zb.joinPath(CODE_PATH, "main.pyw")
RESOURCE_PATH = zb.joinPath(CODE_PATH, "resource")
ICON_PATH = zb.joinPath(RESOURCE_PATH, "program.ico")
SETUP_ISS = zb.joinPath(ROOT, "script", "setup.iss")
INDEX_JSON = zb.joinPath(ROOT, "index.json")
INDEX_HTML = zb.joinPath(ROOT, "index.html")
PYPROJECT_TOML = zb.joinPath(ROOT, "pyproject.toml")
BUILD_PATH = zb.joinPath(ROOT, "build")
SPEC_PATH = zb.joinPath(ROOT, "script", "compile.spec")
