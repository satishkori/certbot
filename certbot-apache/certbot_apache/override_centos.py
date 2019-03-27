""" Distribution specific override class for CentOS family (RHEL, Fedora) """
import logging
import pkg_resources

from acme.magic_typing import List  # pylint: disable=unused-import, no-name-in-module

import zope.interface

from certbot import interfaces


from certbot_apache import apache_util
from certbot_apache import configurator
from certbot_apache import parser
from certbot.errors import MisconfigurationError

logger = logging.getLogger(__name__)


@zope.interface.provider(interfaces.IPluginFactory)
class CentOSConfigurator(configurator.ApacheConfigurator):
    """CentOS specific ApacheConfigurator override class"""

    OS_DEFAULTS = dict(
        server_root="/etc/httpd",
        vhost_root="/etc/httpd/conf.d",
        vhost_files="*.conf",
        logs_root="/var/log/httpd",
        ctl="apachectl",
        version_cmd=['apachectl', '-v'],
        restart_cmd=['apachectl', 'graceful'],
        restart_cmd_alt=['apachectl', 'restart'],
        conftest_cmd=['apachectl', 'configtest'],
        enmod=None,
        dismod=None,
        le_vhost_ext="-le-ssl.conf",
        handle_modules=False,
        handle_sites=False,
        challenge_location="/etc/httpd/conf.d",
        MOD_SSL_CONF_SRC=pkg_resources.resource_filename(
            "certbot_apache", "centos-options-ssl-apache.conf")
    )

    def _prepare_options(self):
        """
        Override the options dictionary initialization in order to support
        alternative restart cmd used in CentOS.
        """
        super(CentOSConfigurator, self)._prepare_options()
        self.options["restart_cmd_alt"][0] = self.option("ctl")

    def get_parser(self):
        """Initializes the ApacheParser"""
        return CentOSParser(
            self.aug, self.option("server_root"), self.option("vhost_root"),
            self.version, configurator=self)

    def _deploy_cert(self, *args, **kwargs):
        """
        Override _deploy_cert in order to ensure that the Apache configuration
        has "LoadModule ssl_module..." before parsing the VirtualHost configuration
        that was created by Certbot
        """
        super(CentOSConfigurator, self)._deploy_cert(*args, **kwargs)
        if self.version < (2, 4, 0):
            self._deploy_loadmodule_ssl_if_needed()


    def _deploy_loadmodule_ssl_if_needed(self):
        """
        Add "LoadModule ssl_module <pre-existing path>" to main httpd.conf if
        it doesn't exist there already.
        """

        loadmods = self.parser.find_dir("LoadModule", "ssl_module", exclude=False)

        correct_ifmods = []  # type: List[str]
        loadmod_args = []  # type: List[str]
        loadmod_paths = []  # type: List[str]
        for m in loadmods:
            noarg_path = m.rpartition("/")[0]
            path_args = self.parser.get_all_args(noarg_path)
            if loadmod_args:
                if loadmod_args != path_args:
                    msg = ("Certbot encountered multiple LoadModule directives "
                           "for LoadModule ssl_module with differing library paths. "
                           "Please remove or comment out the one(s) that are not in "
                           "use, and run Certbot again.")
                    raise MisconfigurationError(msg)
            else:
                loadmod_args = path_args

            if self.parser.not_modssl_ifmodule(noarg_path):  # pylint: disable=no-member
                if self.parser.loc["default"] in noarg_path:
                    # LoadModule already in the main configuration file
                    if "ifmodule/" in noarg_path or "ifmodule[1]" in noarg_path.lower():
                        # It's the first or only IfModule in the file
                        return
                # Populate the list of known !mod_ssl.c IfModules
                nodir_path = noarg_path.rpartition("/directive")[0]
                correct_ifmods.append(nodir_path)
            else:
                loadmod_paths.append(noarg_path)

        if not loadmod_args:
            # Do not try to enable mod_ssl
            return

        # Force creation as the directive wasn't found from the beginning of
        # httpd.conf
        rootconf_ifmod = self.parser.create_ifmod(
            parser.get_aug_path(self.parser.loc["default"]),
            "!mod_ssl.c", beginning=True)
        # parser.get_ifmod returns a path postfixed with "/", remove that
        self.parser.add_dir(rootconf_ifmod[:-1], "LoadModule", loadmod_args)
        correct_ifmods.append(rootconf_ifmod[:-1])
        self.save_notes += "Added LoadModule ssl_module to main configuration.\n"

        # Wrap LoadModule mod_ssl inside of <IfModule !mod_ssl.c> if it's not
        # configured like this already.
        for loadmod_path in loadmod_paths:
            nodir_path = loadmod_path.split("/directive")[0]
            # Remove the old LoadModule directive
            self.aug.remove(loadmod_path)

            # Create a new IfModule !mod_ssl.c if not already found on path
            ssl_ifmod = self.parser.get_ifmod(nodir_path, "!mod_ssl.c",
                                            beginning=True)[:-1]
            if ssl_ifmod not in correct_ifmods:
                self.parser.add_dir(ssl_ifmod, "LoadModule", loadmod_args)
                correct_ifmods.append(ssl_ifmod)
                self.save_notes += ("Wrapped pre-existing LoadModule ssl_module "
                                    "inside of <IfModule !mod_ssl> block.\n")


class CentOSParser(parser.ApacheParser):
    """CentOS specific ApacheParser override class"""
    def __init__(self, *args, **kwargs):
        # CentOS specific configuration file for Apache
        self.sysconfig_filep = "/etc/sysconfig/httpd"
        super(CentOSParser, self).__init__(*args, **kwargs)

    def update_runtime_variables(self):
        """ Override for update_runtime_variables for custom parsing """
        # Opportunistic, works if SELinux not enforced
        super(CentOSParser, self).update_runtime_variables()
        self.parse_sysconfig_var()

    def parse_sysconfig_var(self):
        """ Parses Apache CLI options from CentOS configuration file """
        defines = apache_util.parse_define_file(self.sysconfig_filep, "OPTIONS")
        for k in defines.keys():
            self.variables[k] = defines[k]

    def not_modssl_ifmodule(self, path):
        """Checks if the provided Augeas path has argument !mod_ssl"""

        if "ifmodule" not in path.lower():
            return False

        # Trim the path to the last ifmodule
        workpath = path.lower()
        while workpath:
            # Get path to the last IfModule (ignore the tail)
            parts = workpath.rpartition("ifmodule")

            if not parts[0]:
                # IfModule not found
                break
            ifmod_path = parts[0] + parts[1]
            # Check if ifmodule had an index
            if parts[2].startswith("["):
                # Append the index from tail
                ifmod_path += parts[2].partition("/")[0]
            # Get the original path trimmed to correct length
            # This is required to preserve cases
            ifmod_real_path = path[0:len(ifmod_path)]
            if "!mod_ssl.c" in self.get_all_args(ifmod_real_path):
                return True
            # Set the workpath to the heading part
            workpath = parts[0]

        return False
