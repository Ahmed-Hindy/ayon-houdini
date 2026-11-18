import os
from typing import List, AnyStr

import pyblish.api

from ayon_core.pipeline import KnownPublishError, PublishError
from ayon_core.pipeline.entity_uri import construct_ayon_entity_uri
from ayon_core.pipeline.publish.lib import get_instance_expected_output_path
from ayon_houdini.api import plugin
from ayon_houdini.api.lib import render_rop
from ayon_houdini.api.usd import remap_paths

import hou


def ensure_main_usd_representation(instance, output):
    """Register the USD ROP output once, before references are remapped."""
    expected = {
        "name": "usd",
        "ext": "usd",
        "files": os.path.basename(output),
        "stagingDir": os.path.dirname(output),
    }
    representations = instance.data.setdefault("representations", [])
    matches = [repre for repre in representations
               if repre.get("name") == "usd"]
    if matches:
        if len(matches) != 1 or any(
            matches[0].get(key) != expected[key]
            for key in ("ext", "files", "stagingDir")
        ):
            raise PublishError(
                "A different USD representation already exists for the "
                f"USD ROP output {output!r}."
            )
        return
    representations.append(expected)


class CollectUSDMainRepresentation(plugin.HoudiniInstancePlugin):
    """Plan each main USD ROP output before any USD extraction begins."""

    order = pyblish.api.CollectorOrder + 0.49
    label = "Collect Main USD Representation"
    families = ["usdrop"]

    def process(self, instance):
        ropnode = hou.node(instance.data.get("instance_node"))
        if not ropnode:
            return
        output = ropnode.evalParm("lopoutput")
        if output:
            ensure_main_usd_representation(instance, output)


class ExtractUSD(plugin.HoudiniExtractorPlugin):

    order = pyblish.api.ExtractorOrder
    label = "Extract USD"
    families = ["usdrop"]

    use_ayon_entity_uri = False

    def process(self, instance):

        # Validate all planned outputs before rendering any dependent USD.
        # Otherwise a changed output for a later ROP could be detected only
        # after an earlier ROP has already used its stale mapping.
        self.validate_planned_outputs(instance.context)
        ropnode = hou.node(instance.data.get("instance_node"))

        # Get the filename from the filename parameter
        output = ropnode.evalParm("lopoutput")
        ensure_main_usd_representation(instance, output)
        staging_dir = os.path.dirname(output)
        instance.data["stagingDir"] = staging_dir
        self.log.info("Writing USD '%s' to '%s'" % (
            os.path.basename(output), staging_dir))

        mapping = self.get_source_to_publish_paths(instance.context)
        if mapping:
            self.log.debug(f"Remapping paths: {mapping}")

        # Allow instance-specific path remapping overrides, e.g. changing
        # paths on used resources/textures for looks
        instance_mapping = instance.data.get("assetRemap", {})
        if instance_mapping:
            self.log.debug("Instance-specific asset path remapping:\n"
                           f"{instance_mapping}")
        mapping.update(instance_mapping)

        with remap_paths(ropnode, mapping, expected_output=output):
            render_rop(ropnode)

        # ROP callbacks can change a dependency after the preflight.
        self.validate_planned_outputs(instance.context)
        if not os.path.exists(output):
            raise PublishError(f"Output does not exist: {output}")

    @staticmethod
    def validate_planned_outputs(context):
        """Reject changed or unavailable active USD ROP output paths."""
        for instance in context:
            data = instance.data
            if not data.get("active", True) or not data.get("publish", True):
                continue
            if data.get("family") != "usdrop" and "usdrop" not in (
                    data.get("families") or []):
                continue
            node_path = data.get("instance_node")
            ropnode = hou.node(node_path) if node_path else None
            if ropnode is None:
                raise PublishError(f"USD ROP no longer exists: {node_path!r}")
            output = ropnode.evalParm("lopoutput")
            if not output:
                raise PublishError(
                    f"USD ROP has no output path: {node_path!r}")
            ensure_main_usd_representation(instance, output)

    def get_source_to_publish_paths(self,
                                    context):
        """Define a mapping of all current instances in context from source
        file to publish file so this can be used on the USD save to remap
        asset layer paths on publish via AyonRemapPaths output processor

        Arguments:
            context (pyblish.api.Context): Publish context.

        Returns:
            dict[str, str]: Mapping from source path to remapped path.

        """

        mapping = {}
        for instance in context:
            if not instance.data.get("active", True):
                continue

            if not instance.data.get("publish", True):
                continue

            for repre in instance.data.get("representations", []):
                name = repre.get("name")
                ext = repre.get("ext")

                # TODO: The remapping might need to get more involved if the
                #   asset paths that are set use e.g. $F
                # TODO: If the representation has multiple files we might need
                #   to define the path remapping per file of the sequence
                if self.use_ayon_entity_uri:
                    # Construct AYON entity URI
                    # Note: entity does not exist yet
                    path = construct_ayon_entity_uri(
                        project_name=context.data["projectName"],
                        folder_path=instance.data["folderPath"],
                        product=instance.data["productName"],
                        version=instance.data["version"],
                        representation_name=name
                    )
                else:
                    # Resolved publish filepath
                    path = get_instance_expected_output_path(
                        instance, representation_name=name, ext=ext
                    )

                for source_path in get_source_paths(instance, repre):
                    source_path = os.path.normpath(source_path)
                    mapping[source_path] = path

        return mapping


def get_source_paths(
        instance: pyblish.api.Instance,
        repre: dict
) -> List[AnyStr]:
    """Return the full source filepaths for an instance's representations"""

    staging = repre.get("stagingDir", instance.data.get("stagingDir"))

    # Support special `files_raw` key for representations that may originate
    # from a path in the USD file including `:SDF_FORMAT_ARGS:` which we will
    # also want to match against.
    if "files_raw" in repre:
        files = repre["files_raw"]
    else:
        files = repre.get("files", [])

    if isinstance(files, list):
        return [os.path.join(staging, fname) for fname in files]
    elif isinstance(files, str):
        # Single file
        return [os.path.join(staging, files)]

    raise KnownPublishError(
        "Unsupported type for representation files:"
        f" {files} (supports list or str)"
    )
