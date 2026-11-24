//! Explicit, opt-in Bedrock Converse through the standard AWS SDK credential chain.
use super::{Prediction, decision, prompt};
use crate::commits::QualityPolicy;
use aws_sdk_bedrockruntime::{
    Client,
    config::Region,
    types::{ContentBlock, ConversationRole, InferenceConfiguration, Message},
};
use std::time::Duration;
use tokio::runtime::Runtime;

pub(super) struct Provider {
    client: Client,
    runtime: Runtime,
    model: String,
    max_tokens: i32,
}
impl Provider {
    pub fn new(q: &QualityPolicy) -> Result<Self, String> {
        let runtime = Runtime::new().map_err(|_| "could not initialize Bedrock runtime")?;
        let mut loader = aws_config::defaults(aws_config::BehaviorVersion::latest());
        if let Some(region) = &q.region {
            loader = loader.region(Region::new(region.clone()))
        }
        let config = runtime
            .block_on(async { tokio::time::timeout(Duration::from_secs(30), loader.load()).await })
            .map_err(|_| "could not initialize Bedrock within the provider time limit")?;
        Ok(Self {
            client: Client::new(&config),
            runtime,
            model: q.model.clone(),
            max_tokens: q.max_tokens as i32,
        })
    }
    pub fn predict(&self, text: &str) -> Result<Prediction, String> {
        let message = Message::builder()
            .role(ConversationRole::User)
            .content(ContentBlock::Text(prompt(text)))
            .build()
            .map_err(|_| "could not prepare Bedrock request")?;
        let inference = InferenceConfiguration::builder()
            .max_tokens(self.max_tokens)
            .temperature(0.0)
            .build();
        let request = self
            .client
            .converse()
            .model_id(&self.model)
            .messages(message)
            .inference_config(inference);
        let reply = self
            .runtime
            .block_on(async { tokio::time::timeout(Duration::from_secs(30), request.send()).await })
            .map_err(|_| "Bedrock request exceeded the provider time limit")?
            .map_err(|_| "Bedrock request failed")?;
        let text = reply
            .output()
            .and_then(|o| o.as_message().ok())
            .and_then(|m| m.content().first())
            .and_then(|b| b.as_text().ok())
            .ok_or("Bedrock returned invalid output: expected non-empty text output")?;
        decision(text).map_err(|e| format!("Bedrock returned invalid output: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    #[test]
    fn converse_uses_bounded_selected_text_and_parses_local_reply() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            let mut reader = BufReader::new(&socket);
            let mut request = String::new();
            reader.read_line(&mut request).unwrap();
            assert!(request.starts_with("POST /model/test-model/converse "));
            let mut length = None;
            for line in reader.by_ref().lines() {
                let line = line.unwrap();
                if line.is_empty() {
                    break;
                }
                if let Some((_, value)) = line
                    .split_once(':')
                    .filter(|(s, _)| s.eq_ignore_ascii_case("content-length"))
                {
                    length = Some(value.trim().parse::<usize>().unwrap())
                }
            }
            let length = length.unwrap();
            assert!(length < 4096);
            let mut raw = vec![0; length];
            reader.read_exact(&mut raw).unwrap();
            let doc: serde_json::Value = serde_json::from_slice(&raw).unwrap();
            assert_eq!(
                doc["messages"][0]["content"][0]["text"],
                prompt("fix: selected text")
            );
            assert_eq!(doc["inferenceConfig"]["maxTokens"], 32);
            let body = r#"{"output":{"message":{"role":"assistant","content":[{"text":"FLAG vague change"}]}},"stopReason":"end_turn","usage":{"inputTokens":20,"outputTokens":3,"totalTokens":23},"metrics":{"latencyMs":1}}"#;
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
        });
        let config = aws_sdk_bedrockruntime::Config::builder()
            .behavior_version_latest()
            .region(Region::new("us-east-1"))
            .credentials_provider(aws_sdk_bedrockruntime::config::Credentials::new(
                "fixture",
                "fixture",
                None,
                None,
                "local-fixture",
            ))
            .endpoint_url(format!("http://{address}"))
            .build();
        let provider = Provider {
            client: Client::from_conf(config),
            runtime: Runtime::new().unwrap(),
            model: "test-model".into(),
            max_tokens: 32,
        };
        assert_eq!(
            provider.predict("fix: selected text").unwrap(),
            decision("FLAG vague change").unwrap()
        );
        server.join().unwrap()
    }
}
