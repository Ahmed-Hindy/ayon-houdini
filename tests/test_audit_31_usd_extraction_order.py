"""Verify USD ROP plans are available before dependent extraction."""

import ast
import contextlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest

from pxr import Sdf, Usd

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "client/ayon_houdini/plugins/publish/extract_usd.py"
PROCESSOR = (
    ROOT / "client/ayon_houdini/startup/husdplugins/outputprocessors/"
    "remap_to_publish.py"
)


class PublishError(Exception):
    pass


class Instance:
    def __init__(self, context, name, node):
        self.name = name
        self.context = context
        self.data = {
            "instance_node": node,
            "families": ["usdrop"],
            "productName": name,
            "folderPath": "/asset",
            "version": 1,
        }


class Context(list):
    def __init__(self):
        super().__init__()
        self.data = {"projectName": "Project"}


class TestUsdPlanning(unittest.TestCase):
    def build(self, tmp, uri=False, override=None):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        selected = [
            node
            for node in tree.body
            if (
                isinstance(node, ast.ClassDef)
                and node.name in {"CollectUSDMainRepresentation", "ExtractUSD"}
            )
            or (
                isinstance(node, ast.FunctionDef)
                and node.name
                in {"ensure_main_usd_representation", "get_source_paths"}
            )
        ]
        module = ast.fix_missing_locations(
            ast.Module(body=selected, type_ignores=[])
        )

        class Plugin:
            log = logging.getLogger("test")

        paths = {
            "/out/A": str(Path(tmp) / "A.usda"),
            "/out/B": str(Path(tmp) / "B.usda"),
        }
        rops = {
            path: types.SimpleNamespace(
                path=path, evalParm=lambda name, output=output: output
            )
            for path, output in paths.items()
        }
        context = Context()
        a = Instance(context, "assembly", "/out/A")
        b = Instance(context, "model", "/out/B")
        child = Instance(context, "layer", "/out/unused")
        child.data["families"] = ["usd"]
        child.data["representations"] = [
            {
                "name": "usd",
                "ext": "usd",
                "files": "child.usda",
                "files_raw": "child.usda:SDF_FORMAT_ARGS:a=b",
                "stagingDir": tmp,
            }
        ]
        if override:
            a.data["assetRemap"] = {
                os.path.normpath(paths["/out/B"]): override
            }
        context.extend([a, b, child])
        # A prior export makes the referenced B file a valid USD input.
        initial_stage = Usd.Stage.CreateNew(paths["/out/B"])
        initial_stage.GetRootLayer().Save()
        current_mapping = {}

        @contextlib.contextmanager
        def remap(node, mapping, *, expected_output):
            assert expected_output == paths[node.path]
            current_mapping.clear()
            current_mapping.update(mapping)
            yield

        def render(node):
            output = paths[node.path]
            # Recreate output to prevent stale layers between iterations.
            layer = Sdf.Layer.FindOrOpen(output)
            if layer is None:
                layer = Sdf.Layer.CreateNew(output)
            layer.Clear()
            if node.path == "/out/A":
                prim = Sdf.CreatePrimInLayer(layer, "/Asset")
                prim.specifier = Sdf.SpecifierDef
                reference = current_mapping.get(
                    os.path.normpath(paths["/out/B"])
                )
                if reference is None:
                    reference = paths["/out/B"]
                prim.referenceList.Add(Sdf.Reference(reference))
            layer.Save()

        def expected(instance, representation_name, ext):
            # Require the representation to exist, as originalBasename does.
            rep = next(
                r
                for r in instance.data.get("representations", [])
                if r["name"] == representation_name
            )
            return "/published/{}/{}".format(instance.name, rep["files"])

        env = {
            "os": os,
            "List": list,
            "AnyStr": str,
            "pyblish": types.SimpleNamespace(
                api=types.SimpleNamespace(
                    CollectorOrder=0, ExtractorOrder=2, Instance=Instance
                )
            ),
            "plugin": types.SimpleNamespace(
                HoudiniInstancePlugin=Plugin, HoudiniExtractorPlugin=Plugin
            ),
            "hou": types.SimpleNamespace(node=lambda path: rops.get(path)),
            "PublishError": PublishError,
            "KnownPublishError": PublishError,
            "remap_paths": remap,
            "render_rop": render,
            "get_instance_expected_output_path": expected,
            "construct_ayon_entity_uri": lambda **kw: (
                "ayon://Project/{}/{}".format(
                    kw["product"], kw["representation_name"]
                )
            ),
        }
        exec(compile(module, str(SOURCE), "exec"), env)
        collector = env["CollectUSDMainRepresentation"]()
        extractor = env["ExtractUSD"]()
        extractor.use_ayon_entity_uri = uri
        return context, a, b, child, collector, extractor, paths, env

    def test_both_extraction_orders_keep_references(self):
        for uri in (False, True):
            for reverse in (False, True):
                with self.subTest(uri=uri, reverse=reverse):
                    with tempfile.TemporaryDirectory() as tmp:
                        (
                            context,
                            a,
                            b,
                            child,
                            collector,
                            extractor,
                            paths,
                            env,
                        ) = self.build(tmp, uri=uri)
                        for instance in (a, b):
                            collector.process(instance)
                            collector.process(instance)
                            self.assertEqual(
                                len(instance.data["representations"]), 1
                            )
                        mapped = extractor.get_source_to_publish_paths(context)
                        self.assertIn(
                            os.path.normpath(
                                str(
                                    Path(tmp)
                                    / "child.usda:SDF_FORMAT_ARGS:a=b"
                                )
                            ),
                            mapped,
                        )
                        for instance in (b, a) if reverse else (a, b):
                            extractor.process(instance)
                        layer = Sdf.Layer.FindOrOpen(paths["/out/A"])
                        content = layer.ExportToString()
                        expected = (
                            "ayon://Project/model/usd"
                            if uri
                            else "/published/model/B.usda"
                        )
                        self.assertIn(expected, content)
                        self.assertEqual(len(a.data["representations"]), 1)
                        self.assertEqual(len(b.data["representations"]), 1)

    def test_asset_remap_override_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, a, b, child, collector, extractor, paths, env = (
                self.build(tmp, override="/custom/override.usd")
            )
            collector.process(a)
            collector.process(b)
            extractor.process(a)
            self.assertIn(
                "/custom/override.usd",
                Sdf.Layer.FindOrOpen(paths["/out/A"]).ExportToString(),
            )
            b.data["representations"][0]["files"] = "other.usd"
            with self.assertRaises(PublishError):
                collector.process(b)

    def test_changed_later_output_fails_before_dependent_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, a, b, child, collector, extractor, paths, env = (
                self.build(tmp)
            )
            collector.process(a)
            collector.process(b)
            render_calls = []
            env["render_rop"] = lambda node: render_calls.append(node)
            original_lookup = env["hou"].node
            changed_path = str(Path(tmp) / "changed_B.usda")
            env["hou"].node = lambda path: (
                types.SimpleNamespace(
                    path="/out/B", evalParm=lambda name: changed_path
                )
                if path == "/out/B"
                else original_lookup(path)
            )
            with self.assertRaisesRegex(PublishError, "different USD"):
                extractor.process(a)
            self.assertEqual(render_calls, [])
            self.assertFalse(Path(paths["/out/A"]).exists())

    def test_disabled_usd_instance_does_not_block_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, a, b, child, collector, extractor, paths, env = (
                self.build(tmp)
            )
            collector.process(a)
            collector.process(b)
            b.data["active"] = False
            original_lookup = env["hou"].node
            env["hou"].node = lambda path: (
                None if path == "/out/B" else original_lookup(path)
            )
            extractor.process(a)
            self.assertTrue(Path(paths["/out/A"]).exists())

    def test_dependency_changed_during_render_fails_current_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, a, b, child, collector, extractor, paths, env = (
                self.build(tmp)
            )
            collector.process(a)
            collector.process(b)
            render = env["render_rop"]
            model_rop = env["hou"].node("/out/B")

            def render_then_change_dependency(node):
                render(node)
                model_rop.evalParm = lambda name: str(
                    Path(tmp) / "changed_B.usda"
                )

            env["render_rop"] = render_then_change_dependency
            with self.assertRaisesRegex(PublishError, "different USD"):
                extractor.process(a)
            self.assertTrue(Path(paths["/out/A"]).exists())

    @unittest.skipUnless(importlib.util.find_spec("hou"), "Requires Houdini")
    def test_output_processor_checks_only_the_root_save_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            planned = str(Path(tmp) / "model.usda")
            actual = str(Path(tmp) / "other.usda")
            Path(planned).touch()
            processor = runpy.run_path(str(PROCESSOR))["AYONRemapPaths"]()
            node = types.SimpleNamespace(
                path=lambda: "/out/B",
                evalParm=lambda name: json.dumps({
                    "mapping": {},
                    "expected_output": planned,
                }),
            )
            processor.beginSave(node, {}, None, 0, {})

            self.assertEqual(
                processor.processSavePath(actual, planned, True), actual
            )
            self.assertEqual(
                processor.processSavePath(planned, "", True), planned
            )
            with self.assertRaisesRegex(RuntimeError, "planned output"):
                processor.processSavePath(actual, "", True)


if __name__ == "__main__":
    unittest.main()
