# Copyright (c) 2016-2024 Martin Donath <martin.donath@squidfunk.com>

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import json
import logging
import os
import platform
import requests
import sys

import yaml
from colorama import Fore, Style
from importlib.metadata import distributions, version
from io import BytesIO
from markdown.extensions.toc import slugify
from mkdocs.plugins import BasePlugin, event_priority
from mkdocs.structure.files import get_files
from mkdocs.utils import get_theme_dir, get_yaml_loader
from zipfile import ZipFile, ZIP_DEFLATED

from .config import InfoConfig

# -----------------------------------------------------------------------------
# Classes
# -----------------------------------------------------------------------------

# Info plugin
class InfoPlugin(BasePlugin[InfoConfig]):

    # Initialize plugin
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize incremental builds
        self.is_serve = False

    # Determine whether we're serving the site
    def on_startup(self, *, command, dirty):
        self.is_serve = command == "serve"

    # Create a self-contained example (run earliest) - determine all files that
    # are visible to MkDocs and are used to build the site, create an archive
    # that contains all of them, and print a summary of the archive contents.
    # The author must attach this archive to the bug report.
    @event_priority(100)
    def on_config(self, config):
        if not self.config.enabled:
            return

        # By default, the plugin is disabled when the documentation is served,
        # but not when it is built. This should nicely align with the expected
        # user experience when creating reproductions.
        if not self.config.enabled_on_serve and self.is_serve:
            return

        # If the current working directory isn't a parent of the config file
        # directory then the plugin will not be able to see all the files
        # inside the project. Force the user to run the MkDocs from
        # the correct directory.
        config_dir = os.path.dirname(config.config_file_path)
        if not self.config.root_dir and not config_dir.startswith(os.getcwd()):
            log.error(f"Please run `mkdocs build` from the actual project root")
            self._help_on_bad_cwd(config_dir)
        elif self.config.root_dir:
            self._change_cwd(config_dir)

        # Validate markdown_extensions like Snippets.
        # Read known path config options and validate that they point to
        # children of the current working directory.
        known_path_options = ["base_path", "auto_append", "relative_path"]
        invalid_extensions = []
        for option in known_path_options:
            for ext, cfg in filter(lambda x: option in x[1], config.mdx_configs.items()):
                paths = cfg[option]
                if not isinstance(paths, list):
                    paths = [paths]
                for path in paths:
                    abspath = os.path.abspath(path)
                    if not abspath.startswith(os.getcwd()):
                        invalid_extensions.append((ext, option, abspath))
                    elif not os.path.exists(abspath):
                        invalid_extensions.append((ext, option, abspath))

        if invalid_extensions:
            log.error(f"One or more `markdown_extension` paths are invalid")
            self._help_on_bad_extensions(invalid_extensions)

        # Load the current MkDocs config(s) to get access to INHERIT
        loaded_config = _yaml_load(config.config_file_path)
        if not isinstance(loaded_config, list):
            loaded_config = [loaded_config]

        # Validate different paths to assure that they're within the
        # current working directory.
        paths_to_validate = [
            config.docs_dir,
            *[cfg["INHERIT"] for cfg in filter(lambda x: "INHERIT" in x, loaded_config)]
        ]
        invalid_paths = []
        for path in paths_to_validate:
            if not path.startswith(os.getcwd()):
                invalid_paths.append(path)
            elif not os.path.exists(path):
                invalid_paths.append(path)

        if invalid_paths:
            log.error(f"One or more paths are invalid")
            self._help_on_bad_paths(invalid_paths)

        # Resolve latest version
        url = "https://github.com/squidfunk/mkdocs-material/releases/latest"
        res = requests.get(url, allow_redirects = False)

        # Check if we're running the latest version
        _, current = res.headers.get("location").rsplit("/", 1)
        present = version("mkdocs-material")
        if not present.startswith(current):
            log.error("Please upgrade to the latest version.")
            self._help_on_versions_and_exit(present, current)

        # Exit if archive creation is disabled
        if not self.config.archive:
            sys.exit(1)

        # Print message that we're creating a bug report
        log.info("Started archive creation for bug report")

        # Check that there are no overrides in place - we need to use a little
        # hack to detect whether the custom_dir setting was used without parsing
        # mkdocs.yml again - we check at which position the directory provided
        # by the theme resides, and if it's not the first one, abort.
        if config.theme.dirs.index(get_theme_dir(config.theme.name)):
            log.error("Please remove 'custom_dir' setting.")
            self._help_on_customizations_and_exit()

        # Check that there are no hooks in place - hooks can alter the behavior
        # of MkDocs in unpredictable ways, which is why they must be considered
        # being customizations. Thus, we can't offer support for debugging and
        # must abort here.
        if config.hooks:
            log.error("Please remove 'hooks' setting.")
            self._help_on_customizations_and_exit()

        # Create in-memory archive and prompt author for a short descriptive
        # name for the archive, which is also used as the directory name. Note
        # that the name is slugified for better readability and stripped of any
        # file extension that the author might have entered.
        archive = BytesIO()
        example = input("\nPlease name your bug report (2-4 words): ")
        example, _ = os.path.splitext(example)
        example = "-".join([present, slugify(example, "-")])

        # Create self-contained example from project
        files: list[str] = []
        with ZipFile(archive, "a", ZIP_DEFLATED, False) as f:
            for path in ["mkdocs.yml", "requirements.txt"]:
                if os.path.isfile(path):
                    f.write(path, os.path.join(example, path))

            for cfg in filter(lambda x: "INHERIT" in x, loaded_config):
                path = os.path.relpath(cfg["INHERIT"], os.path.curdir)
                f.write(path, os.path.join(example, path))

            # Append all files visible to MkDocs
            for file in get_files(config):
                path = os.path.relpath(file.abs_src_path, os.path.curdir)
                f.write(path, os.path.join(example, path))

            # Add information on installed packages
            f.writestr(
                os.path.join(example, "requirements.lock.txt"),
                "\n".join(sorted([
                    "==".join([package.name, package.version])
                        for package in distributions()
                ]))
            )

            # Add information on platform
            f.writestr(
                os.path.join(example, "platform.json"),
                json.dumps(
                    {
                        "system": platform.platform(),
                        "python": platform.python_version()
                    },
                    default = str,
                    indent = 2
                )
            )

            # Retrieve list of processed files
            for a in f.filelist:
                files.append("".join([
                    Fore.LIGHTBLACK_EX, a.filename, " ",
                    _size(a.compress_size)
                ]))

        # Finally, write archive to disk
        buffer = archive.getbuffer()
        with open(f"{example}.zip", "wb") as f:
            f.write(archive.getvalue())

        # Print summary
        log.info("Archive successfully created:")
        print(Style.NORMAL)

        # Print archive file names
        files.sort()
        for file in files:
            print(f"  {file}")

        # Print archive name
        print(Style.RESET_ALL)
        print("".join([
            "  ", f.name, " ",
            _size(buffer.nbytes, 10)
        ]))

        # Print warning when file size is excessively large
        print(Style.RESET_ALL)
        if buffer.nbytes > 1000000:
            log.warning("Archive exceeds recommended maximum size of 1 MB")

        # Aaaaaand done
        sys.exit(1)

    # -------------------------------------------------------------------------

    # Print help on versions and exit
    def _help_on_versions_and_exit(self, have, need):
        print(Fore.RED)
        print("  When reporting issues, please first upgrade to the latest")
        print("  version of Material for MkDocs, as the problem might already")
        print("  be fixed in the latest version. This helps reduce duplicate")
        print("  efforts and saves us maintainers time.")
        print(Style.NORMAL)
        print(f"  Please update from {have} to {need}.")
        print(Style.RESET_ALL)
        print(f"  pip install --upgrade --force-reinstall mkdocs-material")
        print(Style.NORMAL)

        # Exit, unless explicitly told not to
        if self.config.archive_stop_on_violation:
            sys.exit(1)

    # Print help on customizations and exit
    def _help_on_customizations_and_exit(self):
        print(Fore.RED)
        print("  When reporting issues, you must remove all customizations")
        print("  and check if the problem persists. If not, the problem is")
        print("  caused by your overrides. Please understand that we can't")
        print("  help you debug your customizations. Please remove:")
        print(Style.NORMAL)
        print("  - theme.custom_dir")
        print("  - hooks")
        print(Fore.YELLOW)
        print("  Additionally, please remove all third-party JavaScript or")
        print("  CSS not explicitly mentioned in our documentation:")
        print(Style.NORMAL)
        print("  - extra_css")
        print("  - extra_javascript")
        print(Style.RESET_ALL)

        # Exit, unless explicitly told not to
        if self.config.archive_stop_on_violation:
            sys.exit(1)

    # Print help on bad execution directory and exit
    def _help_on_bad_cwd(self, config_dir: str):
        print(Fore.RED)
        print("  The current working directory:")
        print(f"    {os.getcwd()}")
        print("  is not a parent of the config file directory:")
        print(f"    {config_dir}")
        print(Style.NORMAL)
        print("  To assure that all project files are found")
        print("  please run the `mkdocs build` command in the actual")
        print("  root directory of the project.\n")
        print("  You can also set the plugin config option `root_dir`")
        print("  with a relative path to the actual root directory.")
        print(Style.RESET_ALL)

        # Exit, unless explicitly told not to
        if self.config.archive_stop_on_violation:
            sys.exit(1)

    # Print help on bad markdown extensions and exit
    def _help_on_bad_extensions(self, bad_extension):
        existing = list(filter(lambda x: os.path.exists(x[-1]), bad_extension))
        not_existing = list(filter(lambda x: not os.path.exists(x[-1]), bad_extension))
        if len(existing) > 0:
            print(Fore.RED)
            print("  The current working (root) directory:")
            print(f"    {os.getcwd()}")
            print("  is not a parent of the following paths:")
            print(Style.NORMAL)
            for snippet in existing:
                print(f"    {':'.join(snippet)}\n")
        if len(not_existing) > 0:
            print(Fore.RED)
            print("  The following files don't exist:")
            print(Style.NORMAL)
            for snippet in not_existing:
                print(f"    {':'.join(snippet)}\n")
        print("  To assure that all project files are found")
        print("  please adjust your config or file structure and")
        print("  put everything within the root directory of the project.")
        print(Style.RESET_ALL)

        # Exit, unless explicitly told not to
        if self.config.archive_stop_on_violation:
            sys.exit(1)

    # Print help on bad paths and exit
    def _help_on_bad_paths(self, bad_paths):
        existing = list(filter(lambda x: os.path.exists(x), bad_paths))
        not_existing = list(filter(lambda x: not os.path.exists(x), bad_paths))
        if len(existing) > 0:
            print(Fore.RED)
            print("  The current working (root) directory:")
            print(f"    {os.getcwd()}")
            print("  is not a parent of the following paths:")
            print(Style.NORMAL)
            for path in existing:
                print(f"    {path}\n")
        if len(not_existing) > 0:
            print(Fore.RED)
            print("  The following files don't exist:")
            print(Style.NORMAL)
            for path in not_existing:
                print(f"    {path}\n")
        print("  To assure that all project files are found")
        print("  please adjust your config or file structure and")
        print("  put everything within the root directory of the project.")
        print(Style.RESET_ALL)

        # Exit, unless explicitly told not to
        if self.config.archive_stop_on_violation:
            sys.exit(1)

    # Change current working directory based on user config
    def _change_cwd(self, config_dir: str):
        if os.path.isabs(self.config.root_dir):
            abspath = self.config.root_dir
        else:
            abspath = os.path.normpath(os.path.join(config_dir, self.config.root_dir))

        if os.path.exists(abspath):
            os.chdir(self.config.root_dir)
        else:
            log.error(f"`root_dir`: {self.config.root_dir} doesn't exist")
            sys.exit(1)

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------

# Print human-readable size
def _size(value, factor = 1):
    color = Fore.GREEN
    if   value > 100000 * factor: color = Fore.RED
    elif value >  25000 * factor: color = Fore.YELLOW
    for unit in ["B", "kB", "MB", "GB", "TB", "PB", "EB", "ZB"]:
        if abs(value) < 1000.0:
            return f"{color}{value:3.1f} {unit}"
        value /= 1000.0

# Custom YAML loader - required to handle the parent INHERIT config.
# It converts the INHERIT path to absolute as a side effect.
# Returns the loaded config, or a list of all loaded configs.
def _yaml_load(source_path: str):

    with open(source_path, "r", encoding="utf-8-sig") as file:
        source = file.read()

    try:
        result = yaml.load(source, Loader=get_yaml_loader()) or {}
    except yaml.YAMLError:
        result = {}

    if "INHERIT" in result:
        relpath = result.get('INHERIT')
        abspath = os.path.normpath(os.path.join(os.path.dirname(source_path), relpath))
        if os.path.exists(abspath):
            result["INHERIT"] = abspath
            log.debug(f"Loading inherited configuration file: {abspath}")
            parent = _yaml_load(abspath)
            if isinstance(parent, list):
                result = [result, *parent]
            elif isinstance(parent, dict):
                result = [result, parent]

    return result if result else {}

# -----------------------------------------------------------------------------
# Data
# -----------------------------------------------------------------------------

# Set up logging
log = logging.getLogger("mkdocs.material.info")
