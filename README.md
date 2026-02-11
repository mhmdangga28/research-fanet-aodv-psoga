# Research program that simulated FANET network 
Researcher : Muhammad Rangga Ridjali </br>
Mentor : Lisa Kristiana

## Table of Contents

* [Overview](#overview-an-open-source-project)
* [Software overview](#software-overview)
* [Getting ns-3](#getting-ns-3)
* [Building ns-3](#building-ns-3)
* [Testing ns-3](#testing-ns-3)
* [Running ns-3](#running-ns-3)
* [ns-3 Documentation](#ns-3-documentation)

> **NOTE**: Much more substantial information about ns-3 can be found at
<https://www.nsnam.org>

## Overview: An Open Source Project

ns-3 is a free open source project aiming to build a discrete-event
network simulator targeted for simulation research and education.
This is a collaborative project; we hope that
the missing pieces of the models we have not yet implemented
will be contributed by the community in an open collaboration
process. If you would like to contribute to ns-3, please check
the [Contributing to ns-3](#contributing-to-ns-3) section below.

This README excerpts some details from a more extensive
tutorial that is maintained at:
<https://www.nsnam.org/documentation/latest/>

## Software overview

From a software perspective, ns-3 consists of a number of C++
libraries organized around different topics and technologies.
Programs that actually run simulations can be written in
either C++ or Python; the use of Python is enabled by
[runtime C++/Python bindings](https://cppyy.readthedocs.io/en/latest/).  Simulation programs will
typically link or import the ns `core` library and any additional
libraries that they need.  ns-3 requires a modern C++ compiler
installation (g++ or clang++) and the [CMake](https://cmake.org) build system.
Most ns-3 programs are single-threaded; there is some limited
support for parallelization using the [MPI](https://www.nsnam.org/docs/models/html/distributed.html) framework.
ns-3 can also run in a real-time emulation mode by binding to an
Ethernet device on the host machine and generating and consuming
packets on an actual network.  The ns-3 APIs are documented
using [Doxygen](https://www.doxygen.nl).

The code for the framework and the default models provided
by ns-3 is built as a set of libraries. The libraries maintained
by the open source project can be found in the `src` directory.
Users may extend ns-3 by adding libraries to the build;
third-party libraries can be found on the [ns-3 App Store](https://www.nsnam.org)
or elsewhere in public Git repositories, and are usually added to the `contrib` directory.

## Getting ns-3

ns-3 can be obtained by either downloading a released source
archive, or by cloning the project's
[Git repository](https://gitlab.com/nsnam/ns-3-dev.git).

Starting with ns-3 release version 3.45, there are two versions
of source archives that are published with each release:

1. ns-3.##.tar.bz2
1. ns-allinone-3.##.tar.bz2

The first archive is simply a compressed archive of the same code
that one can obtain by checking out the release tagged code from
the ns-3-dev Git repository.  The second archive consists of
ns-3 plus additional contributed modules that are maintained outside
of the main ns-3 open source project but that have been reviewed
by maintainers and lightly tested for compatibility with the
release.  The contributed modules included in the `allinone` release
will change over time as new third-party libraries emerge while others
may lose compatibility with the ns-3 mainline (e.g., if they become
unmaintained).

## Building ns-3

As mentioned above, ns-3 uses the CMake build system, but
the project maintains a customized wrapper around CMake
called the `ns3` tool.  This tool provides a
[Waf-like](https://waf.io) API
to the underlying CMake build manager.
To build the set of default libraries and the example
programs included in this package, you need to use the
`ns3` tool. This tool provides a Waf-like API to the
underlying CMake build manager.
Detailed information on how to use `ns3` is included in the
[quick start guide](doc/installation/source/quick-start.rst).

Before building ns-3, you must configure it.
This step allows the configuration of the build options,
such as whether to enable the examples, tests and more.

To configure ns-3 with examples and tests enabled,
run the following command on the ns-3 main directory:

```shell
./ns3 configure --enable-examples --enable-tests
```

Then, build ns-3 by running the following command:

```shell
./ns3 build
```

By default, the build artifacts will be stored in the `build/` directory.

### Supported Platforms

The current codebase is expected to build and run on the
set of platforms listed in the [release notes](RELEASE_NOTES.md)
file.

Other platforms may or may not work: we welcome patches to
improve the portability of the code to these other platforms.

## Testing ns-3

ns-3 contains test suites to validate the models and detect regressions.
To run the test suite, run the following command on the ns-3 main directory:

```shell
./test.py
```

More information about ns-3 tests is available in the
[test framework](doc/manual/source/test-framework.rst) section of the manual.

## Running ns-3

On recent Linux systems, once you have built ns-3 (with examples
enabled), it should be easy to run the sample programs with the
following command, such as:

```shell
./ns3 run simple-global-routing
```

That program should generate a `simple-global-routing.tr` text
trace file and a set of `simple-global-routing-xx-xx.pcap` binary
PCAP trace files, which can be read by `tcpdump -n -tt -r filename.pcap`.
The program source can be found in the `examples/routing` directory.

## Running ns-3 from Python

If you do not plan to modify ns-3 upstream modules, you can get
a pre-built version of the ns-3 python bindings. It is recommended
to create a python virtual environment to isolate different application
packages from system-wide packages (installable via the OS package managers).

```shell
python3 -m venv ns3env
source ./ns3env/bin/activate
pip install ns3
```

If you do not have `pip`, check their documents
on [how to install it](https://pip.pypa.io/en/stable/installation/).

After installing the `ns3` package, you can then create your simulation python script.
Below is a trivial demo script to get you started.

```python
from ns import ns

ns.LogComponentEnable("Simulator", ns.LOG_LEVEL_ALL)

ns.Simulator.Stop(ns.Seconds(10))
ns.Simulator.Run()
ns.Simulator.Destroy()
```

The simulation will take a while to start, while the bindings are loaded.
The script above will print the logging messages for the called commands.

Use `help(ns)` to check the prototypes for all functions defined in the
ns3 namespace. To get more useful results, query specific classes of
interest and their functions e.g., `help(ns.Simulator)`.

Smart pointers `Ptr<>` can be differentiated from objects by checking if
`__deref__` is listed in `dir(variable)`. To dereference the pointer,
use `variable.__deref__()`.

Most ns-3 simulations are written in C++ and the documentation is
oriented towards C++ users. The ns-3 tutorial programs (`first.cc`,
`second.cc`, etc.) have Python equivalents, if you are looking for
some initial guidance on how to use the Python API. The Python
API may not be as full-featured as the C++ API, and an API guide
for what C++ APIs are supported or not from Python do not currently exist.
The project is looking for additional Python maintainers to improve
the support for future Python users.

## ns-3 Documentation

Once you have verified that your build of ns-3 works by running
the `simple-global-routing` example as outlined in the [running ns-3](#running-ns-3)
section, it is quite likely that you will want to get started on reading
some ns-3 documentation.

All of that documentation should always be available from
the ns-3 website: <https://www.nsnam.org/documentation/>.

This documentation includes:

* a tutorial
* a reference manual
* models in the ns-3 model library
* a wiki for user-contributed tips: <https://www.nsnam.org/wiki/>
* API documentation generated using doxygen: this is
  a reference manual, most likely not very well suited
  as introductory text:
  <https://www.nsnam.org/doxygen/index.html>
