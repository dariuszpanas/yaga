//! In-process checker worker for scripts/benchmark_native.py.
use std::{env, fs, hint::black_box, path::Path, time::Instant};
use yaga_core::{
    commits::{check_message, parse_config},
    descriptions::{check_description, policy::parse_description_policy},
};

fn samples(check: impl Fn() -> bool, iterations: u32, measure: bool) -> Result<f64, String> {
    if !check() {
        return Err("unexpected findings during warm Rust API check".into());
    }
    if !measure {
        return Ok(0.0);
    }
    let start = Instant::now();
    for _ in 0..iterations {
        if !black_box(check()) {
            return Err("in-process checker result changed".into());
        }
    }
    Ok(start.elapsed().as_secs_f64() * 1000.0 / f64::from(iterations))
}

fn main() -> Result<(), String> {
    let args: Vec<_> = env::args().skip(1).collect();
    let [family, iterations, measure] = args.as_slice() else {
        return Err("expected commit|description, iterations and 0|1".into());
    };
    let iterations: u32 = iterations.parse().map_err(|_| "invalid iteration count")?;
    if !(1..=1000).contains(&iterations) || !matches!(measure.as_str(), "0" | "1") {
        return Err("expected 1..1000 iterations and measure=0|1".into());
    }
    let measure = measure == "1";
    let read = |name| fs::read_to_string(name).map_err(|e| format!("cannot read {name}: {e}"));
    let elapsed = match family.as_str() {
        "commit" => {
            let policy = parse_config(read(".yaga.toml")?.as_bytes(), Path::new(".yaga.toml"))?;
            let message = read("message.txt")?;
            samples(
                || {
                    check_message(&message, &policy.policy, false, false)
                        .diagnostics
                        .is_empty()
                },
                iterations,
                measure,
            )?
        }
        "description" => {
            let policy = parse_description_policy(read("description.toml")?.as_bytes())?;
            let body = read("body.md")?;
            samples(
                || check_description(&body, &policy, None).is_ok_and(|r| r.diagnostic_count == 0),
                iterations,
                measure,
            )?
        }
        _ => return Err("unsupported benchmark family".into()),
    };
    println!("{elapsed}");
    Ok(())
}
