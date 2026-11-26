//! Explicit, opt-in Bedrock Converse through the standard AWS SDK credential chain.
use super::{Prediction, decision, prompt};
use crate::commits::QualityPolicy;
use aws_sdk_bedrockruntime::{
    Client,
    config::Region,
    operation::converse::ConverseOutput,
    types::{ContentBlock, ConversationRole, InferenceConfiguration, Message, StopReason},
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
        parse_reply(&reply)
    }
}

fn parse_reply(reply: &ConverseOutput) -> Result<Prediction, String> {
    if !matches!(
        reply.stop_reason(),
        StopReason::EndTurn | StopReason::StopSequence
    ) {
        return Err(format!(
            "Bedrock returned incomplete output: stop_reason {}",
            reply.stop_reason().as_str()
        ));
    }
    let message = reply
        .output()
        .and_then(|o| o.as_message().ok())
        .filter(|m| m.role() == &ConversationRole::Assistant)
        .ok_or("Bedrock returned invalid output: expected assistant message")?;
    let mut final_text = None;
    for block in message.content() {
        match block {
            ContentBlock::ReasoningContent(_) => {}
            ContentBlock::Text(text) if final_text.is_none() => final_text = Some(text.as_str()),
            ContentBlock::Text(_) => {
                return Err(
                    "Bedrock returned invalid output: expected a single text content block".into(),
                );
            }
            _ => {
                return Err(
                    "Bedrock returned invalid output: unexpected non-text content block".into(),
                );
            }
        }
    }
    let text =
        final_text.ok_or("Bedrock returned invalid output: expected non-empty text output")?;
    decision(text).map_err(|e| format!("Bedrock returned invalid output: {e}"))
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

    #[test]
    fn parse_reply_validates_stop_reason_and_skips_reasoning_blocks() {
        use aws_sdk_bedrockruntime::types::{
            CachePointBlock, CachePointType, ConverseOutput as ConverseBody, ReasoningContentBlock,
            ReasoningTextBlock,
        };
        let make = |stop_reason: StopReason, content: Vec<ContentBlock>| {
            let msg = Message::builder()
                .role(ConversationRole::Assistant)
                .set_content(Some(content))
                .build()
                .unwrap();
            ConverseOutput::builder()
                .output(ConverseBody::Message(msg))
                .stop_reason(stop_reason)
                .build()
                .unwrap()
        };

        let reasoning = ContentBlock::ReasoningContent(ReasoningContentBlock::ReasoningText(
            ReasoningTextBlock::builder()
                .text("analyzing commit")
                .build()
                .unwrap(),
        ));
        let ok_reply = make(
            StopReason::EndTurn,
            vec![reasoning.clone(), ContentBlock::Text("PASS".into())],
        );
        assert_eq!(parse_reply(&ok_reply).unwrap(), decision("PASS").unwrap());

        let stop_seq = make(
            StopReason::StopSequence,
            vec![ContentBlock::Text("FLAG vague subject".into())],
        );
        assert_eq!(
            parse_reply(&stop_seq).unwrap(),
            decision("FLAG vague subject").unwrap()
        );

        let truncated = make(
            StopReason::MaxTokens,
            vec![ContentBlock::Text("PASS".into())],
        );
        assert!(
            parse_reply(&truncated)
                .unwrap_err()
                .contains("stop_reason max_tokens")
        );

        let guard = make(
            StopReason::GuardrailIntervened,
            vec![ContentBlock::Text("PASS".into())],
        );
        assert!(
            parse_reply(&guard)
                .unwrap_err()
                .contains("guardrail_intervened")
        );

        let cache_point = ContentBlock::CachePoint(
            CachePointBlock::builder()
                .r#type(CachePointType::Default)
                .build()
                .unwrap(),
        );
        let unexpected = make(
            StopReason::EndTurn,
            vec![ContentBlock::Text("PASS".into()), cache_point],
        );
        assert!(
            parse_reply(&unexpected)
                .unwrap_err()
                .contains("unexpected non-text")
        );

        let multi_text = make(
            StopReason::EndTurn,
            vec![
                ContentBlock::Text("PASS".into()),
                ContentBlock::Text("extra".into()),
            ],
        );
        assert!(
            parse_reply(&multi_text)
                .unwrap_err()
                .contains("single text content block")
        );

        let only_reasoning = make(StopReason::EndTurn, vec![reasoning]);
        assert!(parse_reply(&only_reasoning).is_err());
    }
}
