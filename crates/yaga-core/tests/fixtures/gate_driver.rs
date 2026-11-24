//! Run gate operations against the Python contract suite's in-memory FakeApi.
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
use yaga_core::agent_review::github::{
    api::RestApi,
    gate::{self, GateResult, Publisher},
    runs::SourceRun,
};

fn read() -> Result<Value, String> {
    let mut buf = String::new();
    io::stdin()
        .lock()
        .read_line(&mut buf)
        .map_err(|e| e.to_string())?;
    serde_json::from_str(&buf).map_err(|e| e.to_string())
}
fn send(value: &Value) {
    let mut out = io::stdout().lock();
    serde_json::to_writer(&mut out, value).unwrap();
    writeln!(out).unwrap();
    out.flush().unwrap();
}
fn rpc(op: &str, args: Value) -> Result<Value, String> {
    send(&json!({"op":op,"args":args}));
    let reply = read()?;
    if let Some(e) = reply.get("error") {
        return Err(e.as_str().unwrap().into());
    }
    Ok(reply["ok"].clone())
}
struct Fixture;
impl RestApi for Fixture {
    fn get(&mut self, path: &str) -> Result<Value, String> {
        rpc("get", json!([path]))
    }
    fn post(&mut self, path: &str, payload: &Value) -> Result<Value, String> {
        rpc("post", json!([path, payload]))
    }
    fn paginate(&mut self, path: &str) -> Result<Vec<Value>, String> {
        rpc("paginate", json!([path]))?
            .as_array()
            .cloned()
            .ok_or("invalid fixture page".into())
    }
    fn require_success_tail(
        &self,
        count: usize,
        cleanup_margin_seconds: u64,
    ) -> Result<(), String> {
        rpc("reserve", json!([count, cleanup_margin_seconds])).map(|_| ())
    }
}
fn source(s: &Value) -> SourceRun {
    let n = |k: &str| s[k].as_i64().unwrap();
    let t = |k: &str| s[k].as_str().unwrap().to_owned();
    SourceRun {
        run_id: n("run_id"),
        run_attempt: n("run_attempt"),
        run_number: n("run_number"),
        workflow_path: t("workflow_path"),
        head_sha: t("head_sha"),
        conclusion: t("conclusion"),
        created_at: n("created_at"),
        updated_at: n("updated_at"),
        pull_request_number: n("pull_request_number"),
        pull_request_action: t("pull_request_action"),
        base_sha: t("base_sha"),
        head_ref: t("head_ref"),
        head_repository: t("head_repository"),
    }
}
fn run(args: Value) -> Result<GateResult, String> {
    let op = args["operation"].as_str().unwrap();
    let repository = args["repository"].as_str().unwrap();
    let target_url = args["target_url"].as_str().unwrap_or("");
    let mut api = Fixture;
    if op == "invalidate" {
        return gate::invalidate(
            &mut api,
            repository,
            args["event_name"].as_str().unwrap(),
            &args["event"],
            args["run_id"].as_i64().unwrap(),
            args["run_attempt"].as_i64().unwrap(),
            target_url,
        );
    }
    let source = source(&args["source"]);
    let p = Publisher {
        repository,
        source: &source,
        lifecycle_workflow: args["lifecycle_workflow"].as_str().unwrap(),
        server_url: args["server_url"].as_str().unwrap(),
        target_url,
        owner_id: args["direct_author_id"].as_i64().unwrap(),
    };
    let mut wait = || rpc("wait", json!([])).unwrap().as_bool().unwrap();
    match op {
        "prepare" => {
            p.prepare_with_wait(&mut api, args["wake_workflow"].as_str().unwrap(), &mut wait)
        }
        "authorize" => p.authorize(&mut api),
        "finalize" => p.finalize(&mut api),
        "review" => p.review_with_wait(
            &mut api,
            args["allow_request"].as_bool().unwrap(),
            &mut wait,
        ),
        _ => Err("unexpected fixture operation".into()),
    }
}
fn main() {
    let result = run(read().unwrap());
    match result {
        Ok(r) => {
            send(&json!({"result":{"message":r.message,"exit_code":r.exit_code,"route":r.route}}))
        }
        Err(e) => send(&json!({"error":e})),
    }
}
