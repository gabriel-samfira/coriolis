# Copyright 2020 Cloudbase Solutions Srl
# All Rights Reserved.

import re
import json

from oslo_log import log as logging

from coriolis import constants
from coriolis.osmorphing.osdetect import base


LOG = logging.getLogger(__name__)
CENTOS_DISTRO_IDENTIFIER = "CentOS"


class CentOSOSDetectTools(base.BaseLinuxOSDetectTools):

    def detect_os(self):
        os_release = self._get_os_release()
        info = {}
        redhat_release_path = "etc/redhat-release"
        if os_release:
            version = os_release.get("VERSION_ID")
            if not version:
                return {}
            friendly_name = os_release.get("PRETTY_NAME", "%s Version %s" % (
                    CENTOS_DISTRO_IDENTIFIER, version))
        elif self._test_path(redhat_release_path):
            release_info = self._read_file(
                redhat_release_path).decode().splitlines()
            if release_info:
                m = re.match(r"^(.*) release ([0-9].*) \((.*)\).*$",
                             release_info[0].strip())
                if m:
                    distro, version, _ = m.groups()
                    if CENTOS_DISTRO_IDENTIFIER not in distro:
                        LOG.debug(
                            "Distro does not appear to be a CentOS: %s", distro)
                        return {}

                    friendly_name = "%s Version %s" % (
                        CENTOS_DISTRO_IDENTIFIER, version)
        else:
            return {}

        info = {
            "os_type": constants.OS_TYPE_LINUX,
            "distribution_name": CENTOS_DISTRO_IDENTIFIER,
            "release_version": version,
            "friendly_release_name": friendly_name}
        return info
