Inkfall - Linux x86_64 standalone distribution
===============================================

This directory is a self-contained build of Inkfall. It bundles the
Python runtime, PySide6/Qt, the QML components and Inkfall's fonts and
icons; Python and Nuitka are NOT required on the target machine.

Run it
------

    ./inkfall

The "inkfall" script is a thin launcher for the "Inkfall" executable in
this same directory. You can also run ./Inkfall directly.

You can move or extract this directory anywhere you have write access.
Keep the directory contents together: the executable loads PySide6,
qml/ and assets/ from beside itself.

System requirements
-------------------

* x86_64 Linux with a graphical session (X11 or Wayland/XWayland).
* glibc at least the version of the distribution this archive was built
  on. See BUILDING.md in the Inkfall repository for the exact baseline;
  older distributions are not expected to work and have not been tested.
* The following shared libraries are NOT bundled and must come from the
  distribution: libgl1, libegl1, libxkbcommon-x11-0, libxcb-cursor0,
  libdbus-1-3 and libfontconfig1 (package names vary by distribution).

Troubleshooting
---------------

* "error while loading shared libraries": install the libraries listed
  above for your distribution.
* Qt platform plugin errors: make sure a graphical session is available
  and that QT_QPA_PLATFORM is not set to a platform you do not have.
* To check the build without opening a window:

      QT_QPA_PLATFORM=offscreen ./inkfall --self-test
