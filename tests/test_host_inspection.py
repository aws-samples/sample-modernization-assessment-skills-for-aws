import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_workflow import load, review
from harness import completed

host = load("host", "skills/aws-as-is-analysis/scripts/inspect_host.py")
probe = load("probe", "skills/aws-as-is-analysis/scripts/_host_probe.py")
merge_host = load("merge_host", "skills/aws-as-is-analysis/scripts/merge_host_evidence.py")
ACCOUNT = "111111111111"
INSTANCE = "i-0123456789abcdef0"
COMMAND = "11111111-2222-3333-4444-555555555555"


def args_for(root, transport="ssm"):
    return argparse.Namespace(transport=transport, host=None, user=None, port=22,
        known_hosts=root/"known hosts", identity_file=None, instance_id=INSTANCE,
        profile="assessment", environment=False, expected_account=ACCOUNT, region="us-east-1",
        app_root=["/srv/app"], execute=True, output=root/"output")


def sample():
    return {"schema_version":"1.0", "collector":"moda-host-metadata-v1", "platform":"Linux", "status":"observed",
        "processes":[{"pid":12,"name":"node","exe":"/usr/bin/node","cwd":"/srv/app"}],
        "services":{"status":"observed","items":[]}, "containers":{"status":"observed","items":[]},
        "applications":[{"root":"/srv/app","status":"observed","manifests":[],
                         "running_pids_with_matching_cwd":[12],"code_identity":"deployed tree only"}], "limitations":[]}


def aws_reply(args, env, service, operation, *extra):
    if operation == "get-caller-identity":
        return {"Account":ACCOUNT}
    if operation == "describe-instances":
        return {"Reservations":[{"Instances":[{"InstanceId":INSTANCE,"State":{"Name":"running"},"PrivateIpAddress":"10.0.0.10"}]}]}
    if operation == "describe-instance-information":
        return {"InstanceInformationList":[{"InstanceId":INSTANCE,"PingStatus":"Online","PlatformType":"Linux"}]}
    if operation == "send-command":
        return {"Command":{"CommandId":COMMAND}}
    if operation == "get-command-invocation":
        return {"Status":"Success","ResponseCode":0,"StandardOutputContent":json.dumps(sample())}
    raise AssertionError(operation)


def snapshot(root):
    (root/"raw").mkdir(parents=True)
    raw=root/"raw/e0001.json"
    raw.write_text(json.dumps({"Reservations":[{"Instances":[{"InstanceId":INSTANCE}]}]}))
    common={"schema_version":"1.0","run_id":"fixture","account_id":ACCOUNT,"regions":["us-east-1"],"services":["ec2"]}
    inv=dict(common,collection_status="complete",resources=[{
        "id":f"{ACCOUNT}:us-east-1:ec2.instance:{INSTANCE}","type":"ec2.instance","native_id":INSTANCE,
        "name":INSTANCE,"account_id":ACCOUNT,"region":"us-east-1","attributes":{},"observations":{},"evidence_ids":["e0001"]}],relationships=[])
    manifest=dict(common,status="complete",completed_at="2026-10-20T00:00:00Z",truncations=[],coverage={"ok":1},
        read_only=True,api_calls_executed=1,calls=[{"id":"e0001","service":"ec2","operation":"describe-instances",
            "status":"ok","raw_path":"raw/e0001.json","sha256":hashlib.sha256(raw.read_bytes()).hexdigest()}])
    for name,data in [("inventory.json",inv),("collection-manifest.json",manifest)]:
        (root/name).write_text(json.dumps(data))


class HostInspectionTests(unittest.TestCase):
    def test_plan_has_no_aws_or_ssh_execution(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/"planned"
            argv=["inspect_host.py","--transport","ssh","--host","server.example","--user","reader","--output",str(out)]
            with patch.object(sys,"argv",argv), patch.object(host.subprocess,"run") as run:
                self.assertEqual(host.main(),0)
            run.assert_not_called()
            m=json.loads((out/"host-manifest.json").read_text())
            self.assertEqual(m["status"],"planned")
            self.assertIsNone(m["evidence"])

    def test_rejects_target_options_and_out_of_scope_roots(self):
        with tempfile.TemporaryDirectory() as d:
            base=args_for(Path(d),"ssh")
            base.host="10.0.0.10";base.user="reader"
            for changes in [{"host":"-oProxyCommand=bad"},{"user":"reader;bad"},{"app_root":["/"]},
                            {"app_root":["/proc/1"]},{"app_root":["/srv/../etc"]},{"instance_id":"*"}]:
                a=deepcopy(base)
                for k,v in changes.items():setattr(a,k,v)
                with self.subTest(changes=changes),self.assertRaises(ValueError):host.validate_args(a)

    def test_app_root_is_data_not_remote_shell_code(self):
        roots=["/srv/app ' $(touch no-such-file)"]
        text=host.payload(roots)
        # the second line is CONFIG=json.loads(base64.b64decode(<repr bytes>)): decode it without executing anything
        import ast,base64
        line=text.splitlines()[1]
        encoded=ast.literal_eval(line[line.index("b64decode(")+len("b64decode("):line.rindex("))")])
        self.assertEqual(json.loads(base64.b64decode(encoded))["app_roots"],roots)
        compile(text,"probe","exec")

    def test_ssh_requires_known_host_and_disables_config_forwarding(self):
        with tempfile.TemporaryDirectory() as d:
            a=args_for(Path(d),"ssh");a.host="10.0.0.10";a.user="reader"
            with self.assertRaises(ValueError):host.ssh_command(a)
            a.known_hosts.write_text("verified-host-key-placeholder")
            cmd=host.ssh_command(a)
            self.assertIn("StrictHostKeyChecking=yes",cmd)
            self.assertIn("BatchMode=yes",cmd)
            self.assertIn("ForwardAgent=no",cmd)
            self.assertIn("ClearAllForwardings=yes",cmd)
            self.assertEqual(cmd[-2:],["10.0.0.10","python3 -"])
            self.assertTrue(any(x.startswith('UserKnownHostsFile="') for x in cmd))

    def test_account_mismatch_stops_before_remote_execution(self):
        with tempfile.TemporaryDirectory() as d:
            a=args_for(Path(d))
            with patch.object(host,"aws_json",return_value={"Account":"222222222222"}) as aws:
                with self.assertRaisesRegex(ValueError,"account_mismatch"):host.execute(a,{})
            self.assertEqual(aws.call_count,1)

    def test_ssh_ec2_endpoint_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=args_for(Path(d),"ssh");a.host="10.0.0.11";a.user="reader"
            with patch.object(host,"aws_json",side_effect=aws_reply):
                with self.assertRaisesRegex(ValueError,"ssh_host_does_not_match"):host.preflight_aws(a)

    def test_ssm_offline_node_stops_before_send(self):
        def reply(*a):
            r=aws_reply(*a)
            if a[3]=="describe-instance-information":
                r["InstanceInformationList"][0]["PingStatus"]="Offline"
            return r
        with tempfile.TemporaryDirectory() as d,patch.object(host,"aws_json",side_effect=reply) as aws:
            with self.assertRaisesRegex(ValueError,"online_linux"):host.execute(args_for(Path(d)),{})
            self.assertFalse(any(c.args[3]=="send-command" for c in aws.call_args_list))

    def test_ssm_targets_one_node_and_only_retries_status_lookup(self):
        missing=[True]
        def reply(*a):
            if a[3]=="get-command-invocation" and missing:
                missing.pop()
                raise RuntimeError("InvocationDoesNotExist")
            return aws_reply(*a)
        with tempfile.TemporaryDirectory() as d,patch.object(host,"aws_json",side_effect=reply) as aws,patch.object(host.time,"sleep"):
            report={}
            self.assertEqual(host.execute(args_for(Path(d)),report)["status"],"observed")
            sends=[c for c in aws.call_args_list if c.args[3]=="send-command"]
            self.assertEqual(len(sends),1)
            extra=sends[0].args[4:]
            self.assertEqual(extra[extra.index("--instance-ids")+1],INSTANCE)
            self.assertEqual(extra[extra.index("--document-name")+1],"AWS-RunShellScript")
            params=json.loads(extra[extra.index("--parameters")+1])
            self.assertEqual(params["executionTimeout"],["30"])
            self.assertEqual(len(params["commands"]),1)
            self.assertEqual(report["command_id"],COMMAND)

    def test_ssm_poll_timeout_does_not_resubmit(self):
        with tempfile.TemporaryDirectory() as d,patch.object(host,"aws_json",side_effect=aws_reply) as aws:
            with patch.object(host.time,"monotonic",side_effect=[0,61]):
                with self.assertRaisesRegex(RuntimeError,"no_resubmission"):host.execute(args_for(Path(d)),{})
            self.assertEqual(sum(c.args[3]=="send-command" for c in aws.call_args_list),1)

    def test_probe_schema_rejects_secret_fields_and_truncation(self):
        for where in ("top","process","manifest"):
            data=sample()
            if where=="top":data["environ"]={"TOKEN":"secret"}
            elif where=="process":data["processes"][0]["cmdline"]="token=secret"
            else:data["applications"][0]["manifests"]=[{"file":"package.json","scripts":{"start":"secret"}}]
            with self.subTest(where=where),self.assertRaises(ValueError):host.parse_probe(json.dumps(data))
        with self.assertRaises(ValueError):host.parse_probe(json.dumps(sample())[:-5])
        with self.assertRaises(ValueError):host.parse_probe("x"*22000)

    def test_failed_ssh_does_not_persist_stdout_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);known=root/"known";known.write_text("placeholder");out=root/"result"
            argv=["inspect_host.py","--transport","ssh","--host","server.example","--user","reader",
                  "--known-hosts",str(known),"--execute","--output",str(out)]
            with patch.object(sys,"argv",argv),patch.object(host.subprocess,"run",
                    return_value=completed(255,"PRIVATE_MARKER","PRIVATE_MARKER")):
                self.assertEqual(host.main(),1)
            self.assertNotIn("PRIVATE_MARKER","".join(p.read_text() for p in out.rglob("*") if p.is_file()))

    def test_manifest_metadata_filters_scripts_urls_and_source_bodies(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/"package.json").write_text(json.dumps({"name":"orders","version":"1.2.3",
                "scripts":{"start":"PRIVATE_MARKER"}, "dependencies":{"express":"https://user:PRIVATE_MARKER@example/dep"},
                "repository":"PRIVATE_MARKER"}))
            (root/"app.py").write_text("raise Exception('PRIVATE_MARKER')")
            (root/".env").write_text("PASSWORD=PRIVATE_MARKER")
            git=root/".git";git.mkdir();(git/"HEAD").write_text("a"*40)
            with patch.object(probe.subprocess,"run") as run:
                result=probe.inspect_root(root,[{"pid":12,"cwd":str(root.resolve())}])
            run.assert_not_called()
            self.assertNotIn("PRIVATE_MARKER",json.dumps(result))
            self.assertEqual(result["git_commit"],"a"*40)
            self.assertEqual(result["manifests"][0]["dependency_names"],["express"])
            self.assertEqual(result["running_pids_with_matching_cwd"],[12])
            self.assertIn("app.py",result["entry_file_names"])

    def test_manifest_symlinks_and_large_files_are_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);app=root/"app";app.mkdir()
            outside=root/"private";outside.write_text("PRIVATE_MARKER")
            (app/"package.json").symlink_to(outside)
            (app/"requirements.txt").write_text("x"*65537)
            result=probe.inspect_root(app,[])
            self.assertEqual(result["status"],"partial")
            self.assertTrue(all(m.get("status")=="unreadable_or_unsupported" for m in result["manifests"]))
            self.assertNotIn("PRIVATE_MARKER",json.dumps(result))

    def test_host_merge_preserves_original_and_verifies_references(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/"base";snapshot(source);hd=root/"host";hd.mkdir()
            original=(source/"inventory.json").read_bytes()
            report={"schema_version":"1.0","status":"observed","transport":"ssm","instance_id":INSTANCE,
                    "verified_target":{"account_id":ACCOUNT,"region":"us-east-1","instance_id":INSTANCE},
                    "completed_at":"2026-10-20T01:00:00Z","command_id":COMMAND}
            host.write_result(hd,report,sample())
            output=root/"derived"
            self.assertEqual(merge_host.merge(source,hd,output),"h0001")
            self.assertEqual((source/"inventory.json").read_bytes(),original)
            i,m,c=review.verify_inputs(output)
            self.assertEqual(len(i["resources"]),2)
            self.assertEqual(c["h0001"]["command_id"],COMMAND)
            self.assertEqual(i["relationships"][0]["relation"],"observed_on")
            self.assertFalse(m["read_only"])
            with self.assertRaises(ValueError):merge_host.merge(output,hd,root/"duplicate")
            raw=hd/"raw/h0001.json";raw.write_text("{}")
            with self.assertRaisesRegex(ValueError,"checksum"):merge_host.merge(source,hd,root/"tampered")


if __name__=="__main__":
    unittest.main()
