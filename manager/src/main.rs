#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
mod bundle;
mod core;
mod trust;
mod ui;
use clap::Parser;
use std::path::PathBuf;
#[derive(Parser, Clone)]
#[command(version, about = "WPILib single-application offline manager prototype")]
pub struct Args {
    /// Run a JSON CLI operation instead of opening a window.
    #[arg(long,value_parser=["verify","install","update","remove","list","check-updates"])]
    command: Option<String>,
    #[arg(long)]
    bundle: Option<PathBuf>,
    #[arg(long)]
    root: Option<PathBuf>,
    #[arg(long = "package")]
    packages: Vec<String>,
    #[arg(long)]
    profile: Option<String>,
    #[arg(long)]
    accept_licenses: bool,
    /// Native GUI acceptance: capture rendered startup/final frames and exit.
    #[arg(long, hide = true)]
    ui_test_report: Option<PathBuf>,
    #[arg(long,hide=true,value_parser=["verify","install","update","remove"])]
    ui_test_command: Option<String>,
}
impl Args {
    fn request(&self, command: &str) -> core::Request {
        core::Request {
            command: command.into(),
            root: self.root.clone().unwrap_or_else(core::default_root),
            bundle: self.bundle.clone(),
            packages: self.packages.clone(),
            profile: self.profile.clone(),
            accept_licenses: self.accept_licenses,
        }
    }
}
fn main() {
    let args = Args::parse();
    if let Some(command) = &args.command {
        match core::execute(&args.request(command), &|s| eprintln!("{s}")) {
            Ok(value) => println!("{}", value),
            Err(e) => {
                println!(
                    "{}",
                    serde_json::json!({"status":"error","message":e.to_string()})
                );
                std::process::exit(1)
            }
        }
    } else if let Err(e) = ui::run(args) {
        eprintln!("{e}");
        std::process::exit(1)
    }
}
