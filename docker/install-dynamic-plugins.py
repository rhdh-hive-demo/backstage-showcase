#
# Copyright (c) 2023 Red Hat, Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import yaml
import tarfile
import shutil
import subprocess
import base64
import binascii
# This script is used to install dynamic plugins in the Backstage application,
# and is available in the container image to be called at container initialization,
# for example in an init container when using Kubernetes.
#
# It expects, as the only argument, the path to the root directory where
# the dynamic plugins will be installed.
#
# Additionally, the MAX_ENTRY_SIZE environment variable can be defined to set
# the maximum size of a file in the archive (default: 10MB).
#
# The SKIP_INTEGRITY_CHECK environment variable can be defined with ("true") to skip the integrity check of remote packages
#
# It expects the `dynamic-plugins.yaml` file to be present in the current directory and
# to contain the list of plugins to install along with their optional configuration.
#
# The `dynamic-plugins.yaml` file must contain:
#   - a `plugins` list of objects with the following properties:
#     - `package`: the NPM package to install (either a package name or a path to a local package)
#     - `integrity`: a string containing the integrity hash of the package (optional if package is local, as integrity check is not checked for local packages)
#     - `pluginConfig`: an optional plugin-specific configuration fragment
#     - `disabled`: an optional boolean to disable the plugin (`false` by default)
#   - an optional `includes` list of yaml files to include, each file containing a list of plugins.
#
# The plugins listed in the included files will be included in the main list of considered plugins
# and possibly overwritten by the plugins already listed in the main `plugins` list.
#
# For each enabled plugin mentioned in the main `plugins` list and the various included files,
# the script will:
#   - call `npm pack` to get the package archive and extract it in the dynamic plugins root directory
#   - if the package comes from a remote registry, verify the integrity of the package with the given integrity hash
#   - merge the plugin-specific configuration fragment in a global configuration file named `app-config.dynamic-plugins.yaml`
#

class InstallException(Exception):
    """Exception class from which every exception in this library will derive."""
    pass

def merge(source, destination, prefix = ''):
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            merge(value, node, key + '.')
        else:
            # if key exists in destination trigger an error
            if key in destination and destination[key] != value:
                raise InstallException(f"Config key '{ prefix + key }' defined differently for 2 dynamic plugins")

            destination[key] = value

    return destination

RECOGNIZED_ALGORITHMS = (
    'sha512',
    'sha384',
    'sha256',
)

def verify_package_integrity(plugin: dict, archive: str, working_directory: str) -> None:
    package = plugin['package']
    if 'integrity' not in plugin:
        raise InstallException(f'Package integrity for {package} is missing')

    integrity = plugin['integrity']
    if not isinstance(integrity, str):
        raise InstallException(f'Package integrity for {package} must be a string')

    integrity = integrity.split('-')
    if len(integrity) != 2:
        raise InstallException(f'Package integrity for {package} must be a string of the form <algorithm>-<hash>')

    algorithm = integrity[0]
    if algorithm not in RECOGNIZED_ALGORITHMS:
        raise InstallException(f'{package}: Provided Package integrity algorithm {algorithm} is not supported, please use one of following algorithms {RECOGNIZED_ALGORITHMS} instead')

    hash_digest = integrity[1]
    try:
      base64.b64decode(hash_digest, validate=True)
    except binascii.Error:
      raise InstallException(f'{package}: Provided Package integrity hash {hash_digest} is not a valid base64 encoding')

    cat_process = subprocess.Popen(["cat", archive], stdout=subprocess.PIPE)
    openssl_dgst_process = subprocess.Popen(["openssl", "dgst", "-" + algorithm, "-binary"], stdin=cat_process.stdout, stdout=subprocess.PIPE)
    openssl_base64_process = subprocess.Popen(["openssl", "base64", "-A"], stdin=openssl_dgst_process.stdout, stdout=subprocess.PIPE)

    output, _ = openssl_base64_process.communicate()
    print(output.decode('utf-8').strip())
    if hash_digest != output.decode('utf-8').strip():
      raise InstallException(f'{package}: The hash of the downloaded package {output.decode("utf-8").strip()} does not match the provided integrity hash {hash_digest} provided in the configuration file')

def main():
    dynamicPluginsRoot = sys.argv[1]
    maxEntrySize = int(os.environ.get('MAX_ENTRY_SIZE', 10000000))
    skipIntegrityCheck = os.environ.get("SKIP_INTEGRITY_CHECK", "").lower() == "true"

    dynamicPluginsFile = 'dynamic-plugins.yaml'
    dynamicPluginsGlobalConfigFile = os.path.join(dynamicPluginsRoot, 'app-config.dynamic-plugins.yaml')

    # test if file dynamic-plugins.yaml exists
    if not os.path.isfile(dynamicPluginsFile):
        print(f"No {dynamicPluginsFile} file found. Skipping dynamic plugins installation.")
        with open(dynamicPluginsGlobalConfigFile, 'w') as file:
            file.write('')
            file.close()
        exit(0)

    globalConfig = {
      'dynamicPlugins': {
            'rootDirectory': 'dynamic-plugins-root'
      }
    }

    with open(dynamicPluginsFile, 'r') as file:
        content = yaml.safe_load(file)

    if content == '' or content is None:
        print(f"{dynamicPluginsFile} file is empty. Skipping dynamic plugins installation.")
        with open(dynamicPluginsGlobalConfigFile, 'w') as file:
            file.write('')
            file.close()
        exit(0)

    if not isinstance(content, dict):
        raise InstallException(f"{dynamicPluginsFile} content must be a YAML object")

    allPlugins = {}

    if skipIntegrityCheck:
        print(f"SKIP_INTEGRITY_CHECK has been set to {skipIntegrityCheck}, skipping integrity check of packages")
    if 'includes' in content:
        includes = content['includes']
    else:
        includes = []

    if not isinstance(includes, list):
        raise InstallException(f"content of the \'includes\' field must be a list in {dynamicPluginsFile}")

    for include in includes:
        if not isinstance(include, str):
            raise InstallException(f"content of the \'includes\' field must be a list of strings in {dynamicPluginsFile}")

        print('\n======= Including dynamic plugins from', include, flush=True)

        if not os.path.isfile(include):
            raise InstallException(f"File {include} does not exist")

        with open(include, 'r') as file:
            includeContent = yaml.safe_load(file)

        if not isinstance(includeContent, dict):
            raise InstallException(f"{include} content must be a YAML object")

        includePlugins = includeContent['plugins']
        if not isinstance(includePlugins, list):
            raise InstallException(f"content of the \'plugins\' field must be a list in {include}")

        for plugin in includePlugins:
            allPlugins[plugin['package']] = plugin

    if 'plugins' in content:
        plugins = content['plugins']
    else:
        plugins = []

    if not isinstance(plugins, list):
        raise InstallException(f"content of the \'plugins\' field must be a list in {dynamicPluginsFile}")

    for plugin in plugins:
        package = plugin['package']
        if not isinstance(package, str):
            raise InstallException(f"content of the \'plugins.package\' field must be a string in {dynamicPluginsFile}")

        # if `package` already exists in `allPlugins`, then override its fields
        if package not in allPlugins:
            allPlugins[package] = plugin
            continue

        # override the included plugins with fields in the main plugins list
        print('\n======= Overriding dynamic plugin configuration', package, flush=True)
        for key in plugin:
            if key == 'package':
                continue
            allPlugins[package][key] = plugin[key]

    # iterate through the list of plugins
    for plugin in allPlugins.values():
        package = plugin['package']

        if 'disabled' in plugin and plugin['disabled'] is True:
            print('\n======= Skipping disabled dynamic plugin', package, flush=True)
            continue

        print('\n======= Installing dynamic plugin', package, flush=True)

        package_is_local = package.startswith('./')
        if package_is_local:
            package = os.path.join(os.getcwd(), package[2:])
        else:
          # If package is not local, then integrity check is mandatory
          if not skipIntegrityCheck and not plugin['integrity']:
            raise InstallException(f"No integrity hash provided for Package {package}")

        print('\t==> Grabbing package archive through `npm pack`', flush=True)
        completed = subprocess.run(['npm', 'pack', package], capture_output=True, cwd=dynamicPluginsRoot)
        if completed.returncode != 0:
            raise InstallException(f'Error while installing plugin { package } with \'npm pack\' : ' + completed.stderr.decode('utf-8'))

        archive = os.path.join(dynamicPluginsRoot, completed.stdout.decode('utf-8').strip())

        if not package_is_local:
          print('\t==> Verifying package integrity', flush=True)
          verify_package_integrity(plugin, archive, dynamicPluginsRoot)

        directory = archive.replace('.tgz', '')
        directoryRealpath = os.path.realpath(directory)

        print('\t==> Removing previous plugin directory', directory, flush=True)
        shutil.rmtree(directory, ignore_errors=True, onerror=None)
        os.mkdir(directory)

        print('\t==> Extracting package archive', archive, flush=True)
        file = tarfile.open(archive, 'r:gz') # NOSONAR
        # extract the archive content but take care of zip bombs
        for member in file.getmembers():
            if member.isreg():
                if not member.name.startswith('package/'):
                    raise InstallException("NPM package archive archive does not start with 'package/' as it should: " + member.name)

                if member.size > maxEntrySize:
                    raise InstallException('Zip bomb detected in ' + member.name)

                member.name = member.name.removeprefix('package/')
                file.extract(member, path=directory)
            elif member.isdir():
                print('\t\tSkipping directory entry', member.name, flush=True)
            elif member.islnk() or member.issym():
                if not member.linkpath.startswith('package/'):
                  raise InstallException('NPM package archive contains a link outside of the archive: ' + member.name + ' -> ' + member.linkpath)

                member.name = member.name.removeprefix('package/')
                member.linkpath = member.linkpath.removeprefix('package/')

                realpath = os.path.realpath(os.path.join(directory, *os.path.split(member.linkname)))
                if not realpath.startswith(directoryRealpath):
                  raise InstallException('NPM package archive contains a link outside of the archive: ' + member.name + ' -> ' + member.linkpath)

                file.extract(member, path=directory)
            else:
              if member.type == tarfile.CHRTYPE:
                  type_str = "character device"
              elif member.type == tarfile.BLKTYPE:
                  type_str = "block device"
              elif member.type == tarfile.FIFOTYPE:
                  type_str = "FIFO"
              else:
                  type_str = "unknown"

              raise InstallException('NPM package archive contains a non regular file: ' + member.name + ' - ' + type_str)

        file.close()

        print('\t==> Removing package archive', archive, flush=True)
        os.remove(archive)

        if 'pluginConfig' not in plugin:
          print('\t==> Successfully installed dynamic plugin', package, flush=True)
          continue

        # if some plugin configuration is defined, merge it with the global configuration

        print('\t==> Merging plugin-specific configuration', flush=True)
        config = plugin['pluginConfig']
        if config is not None and isinstance(config, dict):
                merge(config, globalConfig)

        print('\t==> Successfully installed dynamic plugin', package, flush=True)

    yaml.safe_dump(globalConfig, open(dynamicPluginsGlobalConfigFile, 'w'))

main()
