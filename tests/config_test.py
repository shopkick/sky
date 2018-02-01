# Copyright (c) Shopkick 2017
# See LICENSE for details.
import tempfile

from opensky import config


def test_pip_pkg():
    assert config.PipPkg.parse('functools32==3.2.3.post2').ver == '3.2.3.post2'
    assert config.PipPkg.parse('functools32==3.2.3.post2').pkg == 'functools32'


skim_yaml = """
---
name: skim_service
type: midtier_v1
owners:
  - Mahmoud Hashemi
repo: 'http://GITLAB_URL/REPO'
pools: {'default': {'instance_group': Backend, 'instance_size': 1}, 'stage': {'TODO:getridofthis': {'instances': 1}}}  # TODO
ports:
  - port: 5000
    name: service
persistence_deps:
    mysql:
        db_name: skim
    redis: true
    zookeeper: true
python_vm: cpy27
flags:
  dev:
    - log_level=DEBUG
  colo1:
    - log_level=INFO
library_deps:
  sky:
    skim_assets: http://GITLAB_URL/REPO/skim_assets.git#138502c5

  # TODO: hooks need to be able to publish their deps, somehow, too.
  conda:
    - anaconda/mysql-python==1.2.5
    - conda-forge/boltons==17.1.0

  pip:
    - sqlalchemy==1.1.12  # required bc mysql is above
    - kazoo==1.3.1
    - clastic==0.4.3
...
"""

def test_port_parsing_with_names():
    with tempfile.NamedTemporaryFile() as fp:
        fp.write(skim_yaml)
        fp.flush()
        result = config.parse(fp.name)
    port_def = result['ports'].pop()
    assert result['ports'] == []
    assert port_def.pop('name') == 'service'
    assert port_def.pop('port') == 5000
    assert port_def == {}


def test_git_ref():
    def roundtrip(url):
        assert config.GitRemoteRef.from_text(url).to_text() == url

    roundtrip(u"USER@GITLAB_URL:REPO")
    roundtrip(u"http://GITLAB_URL/REPO")

