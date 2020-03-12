import base64
import functools
import io
import os
import zipfile

from datetime import datetime
from cumulusci.core.exceptions import TaskOptionsError, CumulusCIException
from cumulusci.core.utils import process_bool_arg, process_list_arg
from cumulusci.salesforce_api.metadata import ApiDeploy
from cumulusci.tasks.salesforce import (
    BaseSalesforceMetadataApiTask,
    BaseSalesforceApiTask,
)
from cumulusci.utils import cd
from cumulusci.utils import temporary_dir
from cumulusci.utils import zip_clean_metaxml
from cumulusci.utils import inject_namespace
from cumulusci.utils import strip_namespace
from cumulusci.utils import tokenize_namespace
from cumulusci.utils import process_text_in_zipfile
from cumulusci.utils.xml import metadata_tree


class Deploy(BaseSalesforceMetadataApiTask):
    api_class = ApiDeploy
    task_options = {
        "path": {
            "description": "The path to the metadata source to be deployed",
            "required": True,
        },
        "unmanaged": {
            "description": "If True, changes namespace_inject to replace tokens with a blank string"
        },
        "namespace_inject": {
            "description": "If set, the namespace tokens in files and filenames are replaced with the namespace's prefix"
        },
        "namespace_strip": {
            "description": "If set, all namespace prefixes for the namespace specified are stripped from files and filenames"
        },
        "namespace_tokenize": {
            "description": "If set, all namespace prefixes for the namespace specified are replaced with tokens for use with namespace_inject"
        },
        "check_only": {
            "description": "If True, performs a test deployment (validation) of components without saving the components in the target org"
        },
        "test_level": {
            "description": "Specifies which tests are run as part of a deployment. Valid values: NoTestRun, RunLocalTests, RunAllTestsInOrg, RunSpecifiedTests."
        },
        "specified_tests": {
            "description": "Comma-separated list of test classes to run upon deployment. Applies only with test_level set to RunSpecifiedTests."
        },
        "static_resource_path": {
            "description": "The path where decompressed static resources are stored.  Any subdirectories found will be zipped and added to the staticresources directory of the build."
        },
        "namespaced_org": {
            "description": "If True, the tokens %%%NAMESPACED_ORG%%% and ___NAMESPACED_ORG___ will get replaced with the namespace.  The default is false causing those tokens to get stripped and replaced with an empty string.  Set this if deploying to a namespaced scratch org or packaging org."
        },
        "clean_meta_xml": {
            "description": "Defaults to True which strips the <packageVersions/> element from all meta.xml files.  The packageVersion element gets added automatically by the target org and is set to whatever version is installed in the org.  To disable this, set this option to False"
        },
    }

    namespaces = {"sf": "http://soap.sforce.com/2006/04/metadata"}

    def _init_options(self, kwargs):
        super(Deploy, self)._init_options(kwargs)

        self.check_only = process_bool_arg(self.options.get("check_only", False))
        self.test_level = self.options.get("test_level")
        if self.test_level and self.test_level not in [
            "NoTestRun",
            "RunLocalTests",
            "RunAllTestsInOrg",
            "RunSpecifiedTests",
        ]:
            raise TaskOptionsError(
                f"Specified test run level {self.test_level} is not valid."
            )

        self.specified_tests = process_list_arg(self.options.get("specified_tests", []))

        if bool(self.specified_tests) != (self.test_level == "RunSpecifiedTests"):
            raise TaskOptionsError(
                f"The specified_tests option and test_level RunSpecifiedTests must be used together."
            )

    def _get_api(self, path=None):
        if not path:
            path = self.task_config.options__path

        package_zip = self._get_package_zip(path)
        self.logger.info("Payload size: {} bytes".format(len(package_zip)))

        return self.api_class(
            self,
            package_zip,
            purge_on_delete=False,
            check_only=self.check_only,
            test_level=self.test_level,
            run_tests=self.specified_tests,
        )

    def _include_directory(self, root_parts):
        # include the root directory, all non-lwc directories and sub-directories, and lwc component directories
        return len(root_parts) == 0 or root_parts[0] != "lwc" or len(root_parts) == 2

    def _include_file(self, root_parts, f):
        if len(root_parts) == 2 and root_parts[0] == "lwc":
            # is file of lwc component directory
            lower_f = f.lower()
            return lower_f.endswith((".js", ".js-meta.xml", ".html", ".css", ".svg"))
        return True

    def _get_files_to_package(self):
        for root, dirs, files in os.walk("."):
            root_parts = root.split(os.sep)[1:]
            if self._include_directory(root_parts):
                for f in files:
                    if self._include_file(root_parts, f):
                        yield os.path.join(root, f)

    def _get_static_resource_files(self):
        for root, dirs, files in os.walk("."):
            for f in files:
                yield os.path.join(root, f)

    def _get_package_zip(self, path):
        # Build the zip file
        zip_bytes = io.BytesIO()
        zipf = zipfile.ZipFile(zip_bytes, "w", zipfile.ZIP_DEFLATED)

        with cd(path):
            for file_to_package in self._get_files_to_package():
                zipf.write(file_to_package)

        zipf.close()

        zipf_processed = self._process_zip_file(zipfile.ZipFile(zip_bytes))
        fp = zipf_processed.fp
        zipf_processed.close()
        return base64.b64encode(fp.getvalue()).decode("utf-8")

    def _process_zip_file(self, zipf):
        zipf = self._process_namespace(zipf)
        zipf = self._process_meta_xml(zipf)
        zipf = self._process_static_resources(zipf)
        return zipf

    def _process_namespace(self, zipf):
        if self.options.get("namespace_tokenize"):
            self.logger.info(
                "Tokenizing namespace prefix {}__".format(
                    self.options["namespace_tokenize"]
                )
            )
            zipf = process_text_in_zipfile(
                zipf,
                functools.partial(
                    tokenize_namespace,
                    namespace=self.options["namespace_tokenize"],
                    logger=self.logger,
                ),
            )
        if self.options.get("namespace_inject"):
            managed = not process_bool_arg(self.options.get("unmanaged", True))
            if managed:
                self.logger.info(
                    "Replacing namespace tokens from metadata with namespace prefix {}__".format(
                        self.options["namespace_inject"]
                    )
                )
            else:
                self.logger.info(
                    "Stripping namespace tokens from metadata for unmanaged deployment"
                )
            zipf = process_text_in_zipfile(
                zipf,
                functools.partial(
                    inject_namespace,
                    namespace=self.options["namespace_inject"],
                    managed=managed,
                    namespaced_org=process_bool_arg(
                        self.options.get("namespaced_org", False)
                    ),
                    logger=self.logger,
                ),
            )
        if self.options.get("namespace_strip"):
            zipf = process_text_in_zipfile(
                zipf,
                functools.partial(
                    strip_namespace,
                    namespace=self.options["namespace_strip"],
                    logger=self.logger,
                ),
            )
        return zipf

    def _process_meta_xml(self, zipf):
        if not process_bool_arg(self.options.get("clean_meta_xml", True)):
            return zipf

        self.logger.info(
            "Cleaning meta.xml files of packageVersion elements for deploy"
        )
        zipf = zip_clean_metaxml(zipf, logger=self.logger)
        return zipf

    def _process_static_resources(self, zip_src):
        relpath = self.options.get("static_resource_path")
        if not relpath or not os.path.exists(relpath):
            return zip_src
        path = os.path.realpath(relpath)

        # We need to build a new zip file so that we can replace package.xml
        zip_dest = zipfile.ZipFile(io.BytesIO(), "w", zipfile.ZIP_DEFLATED)
        for name in zip_src.namelist():
            if name == "package.xml":
                package_xml = zip_src.open(name)
            else:
                content = zip_src.read(name)
                zip_dest.writestr(name, content)

        # Build static resource bundles and add to package zip
        with temporary_dir():
            os.mkdir("staticresources")
            bundles = []
            for name in os.listdir(path):
                bundle_relpath = os.path.join(relpath, name)
                bundle_path = os.path.join(path, name)
                if not os.path.isdir(bundle_path):
                    continue
                self.logger.info(
                    "Zipping {} to add to staticresources".format(bundle_relpath)
                )

                # Add resource-meta.xml file
                meta_name = "{}.resource-meta.xml".format(name)
                meta_path = os.path.join(path, meta_name)
                with open(meta_path, "rb") as f:
                    zip_dest.writestr("staticresources/{}".format(meta_name), f.read())

                # Add bundle
                zip_path = os.path.join("staticresources", "{}.resource".format(name))
                with open(zip_path, "wb") as bundle_fp:
                    bundle_zip = zipfile.ZipFile(bundle_fp, "w", zipfile.ZIP_DEFLATED)
                    with cd(bundle_path):
                        for resource_file in self._get_static_resource_files():
                            bundle_zip.write(resource_file)
                    bundle_zip.close()
                zip_dest.write(zip_path)
                bundles.append(name)

        # Update package.xml
        Package = metadata_tree.parse(package_xml)
        sections = Package.findall("types", name="StaticResource")
        section = sections[0] if sections else None
        if not section:
            section = Package.append("types")
            section.append("name", text="StaticResource")
        for name in bundles:
            section.insert_before(section.find("name"), tag="members", text=name)
        package_xml = Package.tostring(xml_declaration=True)
        zip_dest.writestr("package.xml", package_xml)
        return zip_dest

    def freeze(self, step):
        steps = super(Deploy, self).freeze(step)
        for step in steps:
            if step["kind"] == "other":
                step["kind"] = "metadata"
        return steps


class SharingRecalculationTimeout(CumulusCIException):
    pass


class DeployAndWaitForSharingRecalculation(Deploy, BaseSalesforceApiTask):
    """
    Runs the Deploy task that starts a sharing recalculation then polls checking if sharing recalculation finishes before the timeout.  If the Deploy task does not start a sharing recalculation, this task will time out.
    When a sharing recalculation starts, a SetupAuditTrail record is inserted with Section 'Sharing Defaults' and Action 'owdUpdateStarted'.
    When a sharing recalculation finishes, a SetupAuditTrail record is inserted with Section 'Sharing Defaults' and Action 'owdUpdateFinished'.
    """

    task_options = {
        **Deploy.task_options,
        "timeout": {
            "description": "The max amount of time to wait in seconds.  Default: 600",
            "required": False,
        },
    }

    def _init_options(self, kwargs):
        """Defaults timeout as 600"""
        super(DeployAndWaitForSharingRecalculation, self)._init_options(kwargs)
        if not self.options.get("timeout"):
            self.options["timeout"] = "600"
        self.options["timeout"] = int(self.options["timeout"])

    def _get_last_setup_audit_trail_created_date(self):
        records = self.sf.query(
            "SELECT CreatedDate FROM SetupAuditTrail ORDER BY CreatedDate DESC LIMIT 1"
        ).get("records")
        return records[0].get("CreatedDate") if records else None

    def _log_title(self, title):
        """Logs title with an underline"""
        self.logger.info("")
        self.logger.info(title)
        self.logger.info("-" * len(title))

    def _run_task(self):
        """
        1) Set baselines to detect if:
           - the task times out
           - SetupAuditTrail records inserted due to our deployment
        2) Run the deploy which starts a sharing recalculation
        3) Poll checking if sharing recalculation completes
        """
        self.time_start = datetime.now()
        self.last_setup_audit_trail_created_date = (
            self._get_last_setup_audit_trail_created_date()
        )

        self._log_title("Deploying metadata")

        super(Deploy, self)._run_task()

        if self._is_sharing_recalcuation_started():
            self._log_title(
                f'Checking for {self.options["timeout"]} seconds if sharing recalculation is finished'
            )
            self._poll()
            self.logger.info("Sharing recalculation finished!".format(**self.options))
        else:
            self.log_title("Skipping check for sharing recalculation")
            self.logger.info(
                "A sharing recalculation wasn't started after deploying metadata."
            )

    def _get_sharing_recalculation_query(self):
        """
        When a sharing recalculation finishes, a SetupAuditTrail record is inserted with Section 'Sharing Defaults' and Action 'owdUpdateFinished'.

        If last_setup_audit_trail_created_date is None, then there were no SetupAuditTrail records before Deploy._run_task() was called.
        """
        if self.last_setup_audit_trail_created_date:
            return f"SELECT Action, Section FROM SetupAuditTrail WHERE CreatedDate > {self.last_setup_audit_trail_created_date} ORDER BY CreatedDate DESC"
        return f"SELECT Action, Section FROM SetupAuditTrail ORDER BY CreatedDate DESC"

    def _is_sharing_recalcuation_started(self):
        for record in self.sf.query(self._get_sharing_recalculation_query()).get(
            "records"
        ):
            if (
                record.get("Section") == "Sharing Defaults"
                and record.get("Action") == "owdUpdateStarted"
            ):
                return True
        return False

    def _is_sharing_recalcuation_finished(self):
        for record in self.sf.query(self._get_sharing_recalculation_query()).get(
            "records"
        ):
            if (
                record.get("Section") == "Sharing Defaults"
                and record.get("Action") == "owdUpdateFinished"
            ):
                return True
        return False

    def _poll_action(self):
        """
        When a sharing recalculation finishes, a SetupAuditTrail record is inserted with Section 'Sharing Defaults' and Action 'owdUpdateFinished'.

        To detect if sharing recalculation is finished, we:
        1) Find the latest SetupAuditTrail.CreatedDate before we deploy so we can
           detect all SetupAuditTrail records created after we deploy.
        2) Run the deploy which starts a sharing recalculation.
        3) Query only SetupAuditTrail records created after we deploy.
           Check if there is a record whose Section is 'Sharing Defaults' and
           Action is 'owdUpdateFinised' signifying sharing recalculation is finished.
        """
        elapsed = datetime.now() - self.time_start
        if elapsed.total_seconds() > self.options["timeout"]:
            raise SharingRecalculationTimeout(
                "Sharing recalculation did not finish after {timeout} seconds".format(
                    **self.options
                )
            )

        if self._is_sharing_recalcuation_finished():
            self.poll_complete = True
