# Sky

Sky enables developers to build modern Python applications and
microservices, with built-in support for Anaconda dependencies,
Docker packaging, and DCOS deployment.

# Quick Start

Download the newest version for your platform here:
**[Sky releases](http://GITLAB_HOSTNAME/SKY_RELEASE_REPO)**.

```
chmod a+x ~/Downloads/sky-[ver]
alias sky=~/Downloads/sky-[ver]
sky --help
```

## Installing pre-release/dev versions
```bash
git clone USERNAME@GITLAB_URL:REPO
cd sky
virtualenv build
build/bin/pip install -e .
alias sky="/path/to/sky/build/bin/python -m opensky"
export SKY_SITE_CONFIG=http://GITLAB_URL/REPO
sky pypier install sk-tunnel
```

## Working on sky-based components

Next, clone the git repo of the component you'd like to
develop on, and initialize the environment:

```bash
$ git clone git@GITLAB_URL:REPO
$ cd urlshortener
$ sky setup
```

This will download and set up any necessary library and service dependencies,
from `lib` to `mysql`. Once it's done (5-15 minutes), you're ready to go!

```
$ sky start
```

This starts the component, and any backend service it needs. Future runs of
`sky setup` are only necessary if the `sky.yaml` configuration is changed.

For more commands: `sky --help`!

```
                                                                                  .
       _____ _             _____                                          _     \ _ /
      / ____| |           / ____|                                        | |  -= (_) =-
     | (___ | | ___   _  | |     ___  _ __ ___  _ __ ___   __ _ _ __   __| |    /   \
      \___ \| |/ / | | | | |    / _ \| '_ ` _ \| '_ ` _ \ / _` | '_ \ / _` |    __'  _
      ____) |   <| |_| | | |___| (_) | | | | | | | | | | | (_| | | | | (_| |  _(  )_( )_
     |_____/|_|\_\\__, |  \_____\___/|_| |_| |_|_| |_| |_|\__,_|_| |_|\__,_| (_   _    _)
   __   _          __/ |                                                       (_) (__)
 _(  )_( )_       |___/             __   _           ____       _
(_   _    _)                      _(  )_( )_       |__\_\_o,___/ \
  (_) (__)                       (_   _    _)      ([___\_\_____-\'---</shopkick/<
                                   (_) (__)        | o'
```

# FAQ

## How do I split out a coreservice from the main shopkick repo?

See the [migration guide](MIGRATION_GUIDE.md) next to this document.

## How do I make changes in applications and libraries at the same time?

Codeveloping applications and libraries is a common and necessary
practice here at shopkick. `sky` has special support for modifying
applications (e.g., `urlshortener`) and library dependencies at the
same time (e.g., `lib`). Always better to validate locally before
pushing!

This is the procedure for codevelopment in sky:

1. Clone the lib repo (e.g. to your home directory)
2. In your application repo, `cp sky.yaml sky.local.yaml`
3. In sky.local.yaml, library_deps.sky.[library name], change the
   value from the git remote to the path to your local clone (e.g.,
   "~/[library_name]" if you cloned to your home directory)
4. Restart the application. Note that rerunning `sky setup` is not
   necessary for this change to take effect.

# Background

Sky was created to address the unique needs of shopkick's development
process, present and future. Specifically we wanted to:

* Speed up development-deployment cycles through independent component deploys
* Reduce variance between development and production environments
* Upgrade to modern operating system, Python, and Thrift versions
* Enable a path forward for incremental updates (and usage) of libraries
* Emphasize reproducible development, minimize setup time, and eliminate "works on my machine"
* Leverage new open-source development tools (Docker, DCOS, GitLab)

## The solution

Local development continues on Macs, a necessary evil for iOS app
development. However, server components are run and tested inside a
CentOS 7 + Python 2.7 environment using Mac Docker support. Production
images are built and tested using GitLab CI from the same Dockerfile
used locally, ensuring production and local work the same.

`sky` wraps Docker, conda, pip, DCOS, GitLab, and even certain git
tasks so that developers can focus on development. `sky` becomes a
unified entrypoint, replacing `manage_backends.py`, various test
runners, `make`, `scons`, and more. `sky` turns new engineer onboarding
from a 1-3 day semi-manual process into a 1-3 hour process, fully
automated.

The heaviest dependencies shift from being built from source to being
installed from binary artifacts. We leverage RPMs, conda packages, pip
wheels, and Docker images to reduce build times to the absolute
minimum. These artifacts are explicitly versioned and pinned to
eliminate divergences between local build environments.

Individual GitLab repositories offer more CI and documentation
opportunities. DCOS enables analogous gains for operations, some of
which is exposed through sky. Developers trigger deployments using
GitLab CI, which in turn uses sky to do deployments on DCOS.
Developers can check on their DCOS configuration and status using sky,
as well.

sky development and release also takes place on GitLab. It is shipped
as a single-file executable, and supports plugins for easy
extensibility. sky itself is eminently open-sourceable, and having a
community will help us keep pace with fast-moving Docker changes, as
well as opening up a plugin ecosystem.

## Migration roadmap

Migration begins with coreservices (midtier Thrift services) and their
respective persistence layers. No more downloading, compiling, and
installing Cassandra, MySQL, Redis, Memcache, etc. Thrift is now
automatically compiled at import time, using the newest version of
Thrift. Once coreservices is proven, we move onto Pylons apps. Once
that has momentum, we can shift our attention to refactoring and
revamping `lib`.
