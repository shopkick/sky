# TODO

* self build including all packages in the wheelhouse ends up including duplicates
* init should add .sky to gitignore
* -q/--quiet to turn off all the logging (or maybe just jump it up to the failures)
* pass through args
* integration test
  * still in separate docker image
* DCOS (marathon API) integration
* sky commit and version into sky package
* sky_release json file (or line separated?)
* automate pex release
* update sky README
* logging idea: output dots on actions, unless there is an error or
  failure and then output the original full stack.
* modify localhost:8080 references to pull proxy info from sky_metadata
  * will need to make a dummy SkyMetadata object in get_sky_metadata
    function for opensky and lib development

## Global config notes

* extra PyPI indexes (service_manager.SK_PYPI_URL, shell.SK_PYPI_URL)
* PYTHONPATH?
* cmd: Banner in the plane?
* cache.ANACONDA_URL
* dcos.sky2dcos image location
* dcos.STAGE_HOSTS / dcos.PROD_HOSTS / dcos.ENVIRONMENTS
* deployment timeouts / canaries / health checks (gonna need a strategy pattern here maybe)
* dcos.ENVIRON_VIPS
* dcos.CPU/MEM_SLICE
* populate (plugin)
* docker.CENTOS (base image), docker.username, docker.uid
* docker.BUILT_INS
* service_manager.HOST_PORTS
* proxy / tunnel  (requests + ALL_PROXY env var)
* list of plugins?
* need a "plugins" section for when plugins need a site config value?

## self fail

* Internal error: 1/0
* Command error: bash false
* Docker build error
* Docker run error

## Site Config Services note

At the time of writing, we currently still load service dependencies
from the BUILT_INS map. Ports are not shared. If we switch to site
config's services, then a service which depends on two other services
would each a MySQL wanting to use 3306. We'll have to answer the
question as to whether or not to coordinate sharing these services
(merging service dependencies), or clearing out ports, giving each
service its own service. The former is more like the current system
and is a reasonable approach, it seems.

## Plugins

Functionality:

* Subcommands + arguments
  * Have the opportunity to react to global flags (help, verbosity, version)
* Config schema
* Startup action (check / set up tunnel, hit analytics endpoint, check for updates)

Furthermore, site config will probably need a plugins section, and sky
will need a way to check plugin upgradability.

# Note

TODO on anaconda/gcc in META_REQUIREMENTS because twisted needs gcc to
install for ncolony. We should aim to remove gcc from the runtime
image.

# Longer-term

* Log/analytics/bug reporting
