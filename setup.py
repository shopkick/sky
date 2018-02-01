import os.path
import datetime
import subprocess

from setuptools import setup, find_packages
from setuptools.command.build_py import build_py as BuildPyCommandBase
from setuptools.command.sdist import sdist as SdistCommandBase

__author__ = 'Kurt Rose and Mahmoud Hashemi'
__version__ = '17.8.4'
__contact__ = 'devops@shopkick.com'
__url__ = 'https://github.com/shopkick/sky'

CUR_PATH = os.path.dirname(os.path.abspath(__file__))

# if we want to skinny this up, we can remove the build deps as those
# can be installed into a virtualenv at build time
INSTALL_REQUIRES = [
    'ashes', 'attrs', 'boltons', 'colorama', 'gather', 'hyperlink',
    'jsonpatch', 'lithoxyl', 'marathon', 'PySocks',
    'PyYAML', 'schema', 'seashore', 'requests', 'ptpython', 'venusian',
    'virtualenv',
]


def get_revision_info():
    git_rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=CUR_PATH)
    git_rev = git_rev.strip()

    git_rev_name = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                           cwd=CUR_PATH)
    git_rev_name = git_rev_name.strip()

    git_rev_timestamp = subprocess.check_output(['git', 'rev-list', '--format=format:%ai',
                                                 '--max-count=1', git_rev], cwd=CUR_PATH)
    git_rev_timestamp = git_rev_timestamp.strip().splitlines()[-1]

    return git_rev, git_rev_name, git_rev_timestamp


def write_version_module():
    try:
        git_rev, git_rev_name, git_rev_timestamp = get_revision_info()
    except subprocess.CalledProcessError:
        print '!! Failed to generate version information !!'
        return
    with open(CUR_PATH + '/opensky/version.py', 'wb') as f:
        f.write("version = %r\n" % __version__)
        f.write("revision = %r\n" % git_rev)
        f.write("revision_name = %r\n" % git_rev_name)
        f.write("revision_timestamp = %r\n" % git_rev_timestamp)
        f.write("build_timestamp = %r\n" % datetime.datetime.now().isoformat())
    return


class BuildPyCommand(BuildPyCommandBase):
    def run(self):
        write_version_module()
        return BuildPyCommandBase.run(self)


class SdistCommand(SdistCommandBase):
    def run(self):
        write_version_module()
        return SdistCommandBase.run(self)


GOES_IN_DOCKER_IMAGE = [
  'goes_in_docker_image/' + fname for fname in
  os.listdir(CUR_PATH + '/opensky/goes_in_docker_image/')]
GOES_IN_DOCKER_IMAGE += ['goes_in_docker_image/debug/sitecustomize.py']


if __name__ == '__main__':
    setup(name='opensky',
          cmdclass={'build_py': BuildPyCommand,
                    'sdist': SdistCommand},
          version=__version__,
          description="Next-generation command-line interface to shopkick development.",
          author=__author__,
          author_email=__contact__,
          url=__url__,
          packages=find_packages(),
          package_data={'opensky': GOES_IN_DOCKER_IMAGE},
          entry_points={'gather': ['dummy=opensky:dummy']},
          extras_require={ 'test': ['pytest', 'pylint', 'flake8'] },
          include_package_data=True,
          install_requires=INSTALL_REQUIRES
         )
