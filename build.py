#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build, Test and Deploy Docker images for Conan project"""
import collections
import os
import logging
import subprocess
import re
import requests
import time
from humanfriendly import format_size
from conans import __version__ as client_version
from cpt.ci_manager import CIManager
from cpt.printer import Printer


class ConanDockerTools(object):
    """Execute all build process for Docker image
    """

    def __init__(self):
        logging.basicConfig(format='%(asctime)s [%(levelname)s]: %(message)s', level=logging.INFO)

        self.variables = self._get_variables()

        filter_gcc_compiler_version = self.variables.gcc_versions
        filter_clang_compiler_version = self.variables.clang_versions

        Compiler = collections.namedtuple("Compiler", "name, versions")
        self.gcc_compiler = Compiler(name="gcc", versions=filter_gcc_compiler_version)
        self.clang_compiler = Compiler(name="clang", versions=filter_clang_compiler_version)
        self.loggedin = False
        self.service = None

        logging.info("""
    The follow compiler versions will be built:
        GCC: %s
        CLANG: %s
        """ % (self.gcc_compiler.versions, self.clang_compiler.versions))

    def _get_variables(self):
        """Load environment variables to configure
        :return: Variables
        """
        docker_upload = self._get_boolean_var("DOCKER_UPLOAD")
        docker_upload_only_when_stable = self._get_boolean_var("DOCKER_UPLOAD_ONLY_WHEN_STABLE", "true")
        build_server = self._get_boolean_var("BUILD_CONAN_SERVER_IMAGE")
        docker_password = os.getenv("DOCKER_PASSWORD", "").replace('"', '\\"')
        docker_username = os.getenv("DOCKER_USERNAME", "conanio")
        docker_login_username = os.getenv("DOCKER_LOGIN_USERNAME", "lasote")
        docker_build_tag = os.getenv("DOCKER_BUILD_TAG", "latest")
        docker_archs = os.getenv("DOCKER_ARCHS").split(",") if os.getenv("DOCKER_ARCHS") else [
            "x86_64"
        ]
        docker_distro = os.getenv("DOCKER_DISTRO", False)
        conan_version = os.getenv("CONAN_VERSION", client_version)
        os.environ["CONAN_VERSION"] = conan_version
        os.environ["DOCKER_USERNAME"] = docker_username
        os.environ["DOCKER_BUILD_TAG"] = docker_build_tag
        gcc_versions = os.getenv("GCC_VERSIONS").split(",") if os.getenv("GCC_VERSIONS") else []
        clang_versions = os.getenv("CLANG_VERSIONS").split(",") \
            if os.getenv("CLANG_VERSIONS") else []

        Variables = collections.namedtuple(
            "Variables", "docker_upload, docker_password, "
            "docker_username, docker_login_username, "
            "gcc_versions, docker_distro, "
            "clang_versions, build_server, "
            "docker_build_tag, docker_archs, docker_upload_only_when_stable")
        return Variables(docker_upload, docker_password, docker_username, docker_login_username,
                         gcc_versions, docker_distro, clang_versions, build_server,
                         docker_build_tag, docker_archs, docker_upload_only_when_stable)

    def _get_boolean_var(self, var, default="false"):
        """ Parse environment variable as boolean type
        :param var: Environment variable name
        """
        return os.getenv(var, default.lower()).lower() in ["1", "true", "yes"]

    def login(self):
        if self.variables.docker_upload_only_when_stable:
            printer = Printer()
            ci_manager = CIManager(printer)
            if ci_manager.get_branch() != "master" or ci_manager.is_pull_request():
                logging.info("Skipped login, is not stable branch")
                return

        if not self.variables.docker_upload:
            logging.info("Skipped login, DOCKER_UPLOAD is not activated")
            return

        if not self.variables.docker_password:
            logging.warning("Skipped login, DOCKER_PASSWORD is missing!")
            return

        if not self.variables.docker_login_username:
            logging.warning("Skipped login, DOCKER_LOGIN_USERNAME is missing!")
            return

        logging.info("Login to Docker hub account")
        result = subprocess.call([
            "docker", "login", "-u", self.variables.docker_login_username, "-p",
            self.variables.docker_password
        ])
        if result != os.EX_OK:
            raise RuntimeError("Could not login username %s "
                               "to Docker hub." % self.variables.docker_login_username)

        logging.info("Logged in Docker hub account with success")
        self.loggedin = True

    @property
    def created_image_name(self):
        return "%s/%s:%s" % (self.variables.docker_username,
                             self.service,
                             self.variables.docker_build_tag)

    @property
    def tagged_image_name(self):
        return "%s/%s:%s" % (self.variables.docker_username,
                             self.service,
                             client_version)

    def build(self):
        """Call docker build to create a image
        :param service: service in compose e.g gcc54
        """
        logging.info("Starting build for service %s." % self.service)
        # --no-cache
        subprocess.check_call("docker-compose build --no-cache %s" % self.service, shell=True)

        output = subprocess.check_output("docker image inspect %s --format '{{.Size}}'"
            % self.created_image_name, shell=True)
        size = int(output.decode().strip())
        logging.info("%s image size: %s" % (self.created_image_name, format_size(size)))

    def linter(self, build_dir):
        """Execute hadolint to check possible prone errors

        :param build_dir: Directory with Dockerfile
        """
        logging.info("Executing hadolint on directory %s." % build_dir)
        subprocess.call(
            'docker run --rm -i lukasmartinelli/hadolint < %s/Dockerfile' % build_dir, shell=True)

    def test(self, arch, compiler_name, compiler_version, distro):
        """Validate Docker image by Conan install
        :param arch: Name of he architecture
        :param compiler_name: Compiler to be specified as conan setting e.g. clang
        :param compiler_version: Compiler version to be specified as conan setting e.g. 3.8
        :param service: Docker compose service name
        :param distro: Use other linux distro
        """
        logging.info("Testing Docker by service %s." % self.service)
        try:
            libcxx_list = ["libstdc++"] if compiler_name == "gcc" else ["libstdc++", "libc++"]
            sudo_commands = ["", "sudo"] if distro else ["", "sudo", "sudo -E"]
            subprocess.check_call("docker run -t -d --name %s %s" % (self.service,
                self.created_image_name), shell=True)

            for sudo_command in sudo_commands:

                logging.info("Testing command prefix: '{}'".format(sudo_command))
                output = subprocess.check_output(
                    "docker exec %s %s python3 --version" % (self.service, sudo_command), shell=True)
                assert "Python 3" in output.decode()
                logging.info("Found %s" % output.decode().rstrip())

                output = subprocess.check_output(
                    "docker exec %s %s pip --version" % (self.service, sudo_command), shell=True)
                assert "python 3" in output.decode()
                logging.info("Found pip (Python 3)")

                output = subprocess.check_output(
                    "docker exec %s %s pip3 --version" % (self.service, sudo_command), shell=True)
                assert "python 3" in output.decode()
                logging.info("Found pip3 (Python 3)")

                output = subprocess.check_output(
                    "docker exec %s %s pip show conan" % (self.service, sudo_command), shell=True)
                assert "python3" in output.decode()
                logging.info("Found Conan (Python 3)")

                output = subprocess.check_output(
                    "docker exec %s %s python --version" % (self.service, sudo_command), shell=True)
                assert "Python 3" in output.decode()
                logging.info("Default Python version: %s" % output.decode().rstrip())

                for module in ["lzma", "sqlite3", "bz2", "zlib", "readline"]:
                    subprocess.check_call(
                        'docker exec %s %s python -c "import %s"' % (self.service, sudo_command, module),
                        shell=True)

                subprocess.check_call(
                    "docker exec %s %s pip install --no-cache-dir -U conan_package_tools" %
                    (self.service, sudo_command),
                    shell=True)
                subprocess.check_call(
                    "docker exec %s %s pip install --no-cache-dir -U conan" % (self.service,
                                                                               sudo_command),
                    shell=True)
                subprocess.check_call("docker exec %s conan user" % self.service, shell=True)

            if compiler_name == "clang" and compiler_version == "7":
                compiler_version = "7.0"  # FIXME: Remove this when fixed in conan

            subprocess.check_call(
                "docker exec %s conan install lz4/1.8.3@bincrafters/stable -s "
                "arch=%s -s compiler=%s -s compiler.version=%s --build" %
                (self.service, arch, compiler_name, compiler_version),
                shell=True)

            for libcxx in libcxx_list:
                subprocess.check_call(
                    "docker exec %s conan install gtest/1.8.1@bincrafters/stable -s "
                    "arch=%s -s compiler=%s -s compiler.version=%s "
                    "-s compiler.libcxx=%s --build" % (self.service, arch, compiler_name,
                                                       compiler_version, libcxx),
                    shell=True)

            if "arm" in arch:
                logging.warn("Skipping cmake_installer: cross-building results in Unverified HTTPS error")
            else:
                subprocess.check_call(
                    "docker exec %s conan install cmake_installer/3.13.0@conan/stable -s "
                    "arch_build=%s -s os_build=Linux --build" % (self.service, arch),
                    shell=True)

        finally:
            subprocess.call("docker stop %s" % self.service, shell=True)
            subprocess.call("docker rm %s" % self.service, shell=True)

    def test_server(self):
        """Validate Conan Server image
        :param service: Docker compose service name
        """
        logging.info("Testing Docker running service %s." % self.service)
        try:
            subprocess.check_call(
                "docker run -t -d -p 9300:9300 --name %s %s" % (self.service,
                    self.created_image_name), shell=True)
            time.sleep(3)
            response = requests.get("http://0.0.0.0:9300/v1/ping")
            assert response.ok
        finally:
            subprocess.call("docker stop %s" % self.service, shell=True)
            subprocess.call("docker rm %s" % self.service, shell=True)

    def deploy(self):
        """Upload Docker image to dockerhub
        :param service: Service that contains the docker image
        """
        if not self.loggedin:
            logging.info("Skipping upload. Docker account is not connected.")
            return

        logging.info("Upload Docker image from service %s to Docker hub." % self.service)
        subprocess.check_call("docker-compose push %s" % self.service, shell=True)
        logging.info("Upload Docker image %s" % self.tagged_image_name)
        subprocess.check_call("docker push %s" % self.tagged_image_name, shell=True)

    def tag(self):
        """Apply Docker tag name
        :param service: Docker tag
        """
        logging.info("Creating Docker tag %s" % self.tagged_image_name)
        subprocess.check_call("docker tag %s %s" % (self.created_image_name,
            self.tagged_image_name), shell=True)

    def run(self):
        """Execute all 3 stages for all versions in compilers list
        """
        distro = "" if not self.variables.docker_distro else "-%s" % self.variables.docker_distro
        for arch in self.variables.docker_archs:
            for compiler in [self.gcc_compiler, self.clang_compiler]:
                for version in compiler.versions:
                    tag_arch = "" if arch == "x86_64" else "-%s" % arch
                    service = "%s%s%s%s" % (compiler.name, version.replace(".", ""), distro, tag_arch)
                    build_dir = "%s_%s%s%s" % (compiler.name, version, distro, tag_arch)

                    self.service = service
                    self.login()
                    self.linter(build_dir)
                    self.build()
                    self.tag()
                    self.test(arch, compiler.name, version, self.variables.docker_distro)
                    self.deploy()

        self.service = image_name = "conan_server"
        if self.variables.build_server:
            logging.info("Bulding %s image..." % image_name)
            self.login()
            self.linter(self.service)
            self.build()
            self.test_server()
            self.tag()
            self.deploy()
        else:
            logging.info("Skipping %s image creation" % image_name)


if __name__ == "__main__":
    conan_docker_tools = ConanDockerTools()
    conan_docker_tools.run()
