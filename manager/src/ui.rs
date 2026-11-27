use crate::{core, Args};
use eframe::egui;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    time::Instant,
};
enum Event {
    Log(String),
    Done(Result<Value, String>),
}
struct App {
    args: Args,
    root: String,
    bundle: String,
    dialog: egui_file_dialog::FileDialog,
    pick_root: bool,
    selected: BTreeSet<String>,
    profile: String,
    accepted: bool,
    logs: Vec<String>,
    result: Option<Value>,
    error: Option<String>,
    rx: Option<mpsc::Receiver<Event>>,
    verified: Option<Value>,
    started: Instant,
    frames: u32,
    start_capture: Arc<AtomicBool>,
    final_capture: bool,
    auto_started: bool,
}
impl App {
    fn new(args: Args) -> Self {
        Self {
            root: args
                .root
                .clone()
                .unwrap_or_else(core::default_root)
                .display()
                .to_string(),
            bundle: args
                .bundle
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_default(),
            selected: args.packages.iter().cloned().collect(),
            profile: args.profile.clone().unwrap_or_default(),
            accepted: args.accept_licenses,
            args,
            dialog: egui_file_dialog::FileDialog::new(),
            pick_root: false,
            logs: vec![],
            result: None,
            error: None,
            rx: None,
            verified: None,
            started: Instant::now(),
            frames: 0,
            start_capture: Arc::new(AtomicBool::new(false)),
            final_capture: false,
            auto_started: false,
        }
    }
    fn launch(&mut self, command: &str, ctx: &egui::Context) {
        let request = core::Request {
            command: command.into(),
            root: PathBuf::from(&self.root),
            bundle: if self.bundle.is_empty() {
                None
            } else {
                Some(PathBuf::from(&self.bundle))
            },
            packages: self.selected.iter().cloned().collect(),
            profile: if command == "install" && !self.profile.is_empty() {
                Some(self.profile.clone())
            } else {
                None
            },
            accept_licenses: self.accepted,
        };
        let (tx, rx) = mpsc::channel();
        self.rx = Some(rx);
        self.result = None;
        self.error = None;
        self.logs.clear();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let result = core::execute(&request, &|line| {
                let _ = tx.send(Event::Log(line));
                ctx.request_repaint();
            })
            .map_err(|e| e.to_string());
            let _ = tx.send(Event::Done(result));
            ctx.request_repaint();
        });
    }
    fn capture(&self, ctx: &egui::Context, phase: &str, native: bool) {
        let Some(report) = self.args.ui_test_report.clone() else {
            return;
        };
        let phase = phase.to_owned();
        let image_path = report.with_extension(format!("{phase}.png"));
        let result = self.result.clone();
        let logs = self.logs.clone();
        let error = self.error.clone();
        let first = self.start_capture.clone();
        let elapsed = self.started.elapsed().as_millis();
        let ctx = ctx.clone();
        let ctx_done = ctx.clone();
        ctx.request_screenshot(move|pixels|{let run=||->Result<(),Box<dyn std::error::Error>>{if let Some(p)=report.parent(){std::fs::create_dir_all(p)?;}let data:Vec<u8>=pixels.pixels.iter().flat_map(|p|p.to_array()).collect();image::save_buffer(&image_path,&data,pixels.size[0] as u32,pixels.size[1] as u32,image::ColorType::Rgba8)?;
            std::fs::write(if phase=="startup"{report.with_extension("startup.json")}else{report.clone()},serde_json::to_vec_pretty(&json!({"native_window":native,"phase":phase,"elapsed_ms":elapsed,"size":pixels.size,"screenshot":image_path,"result":result,"error":error,"logs":logs}))?)?;Ok(())};if let Err(e)=run(){eprintln!("Screenshot/report failed: {e}");}first.store(true,Ordering::SeqCst);if phase=="final"{ctx_done.send_viewport_cmd(egui::ViewportCommand::Close)}ctx_done.request_repaint();});
    }
}
impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        self.frames += 1;
        let ctx = ui.ctx().clone();
        let mut done = false;
        if let Some(rx) = &self.rx {
            while let Ok(event) = rx.try_recv() {
                match event {
                    Event::Log(line) => {
                        if self.logs.len() > 300 {
                            self.logs.remove(0);
                        }
                        self.logs.push(line);
                    }
                    Event::Done(result) => {
                        match result {
                            Ok(v) => {
                                if v.get("packages").is_some() {
                                    self.verified = Some(v.clone())
                                } else {
                                    self.verified = None;
                                }
                                self.result = Some(v)
                            }
                            Err(e) => {
                                self.verified = None;
                                self.error = Some(e);
                            }
                        }
                        done = true;
                    }
                }
            }
        }
        if done {
            self.rx = None;
        }
        egui::CentralPanel::default().show(ui,|ui|{
            ui.heading("WPILib Manager");ui.label("Offline installation and maintenance · Rust + private Qt prototype");ui.separator();
            ui.add_enabled_ui(self.rx.is_none(),|ui|{
                ui.horizontal(|ui|{ui.label("Installation folder");if ui.text_edit_singleline(&mut self.root).changed(){self.verified=None;}if ui.button("Browse folder…").clicked(){self.pick_root=true;self.dialog.pick_directory();}});
                ui.horizontal(|ui|{ui.label("Offline bundle ZIP");if ui.text_edit_singleline(&mut self.bundle).changed(){self.verified=None;}if ui.button("Browse bundle…").clicked(){self.pick_root=false;self.dialog.pick_file();}});
                ui.small("Leave the bundle empty to use the verified media retained by the installed manager.");
                if ui.button("Verify bundle / check for updates").clicked(){self.launch("verify",&ctx);}
                if let Some(v)=self.verified.clone(){
                    ui.label(format!("Verified registry revision {}",v["registry"]["registry_revision"]));
                    if let Some(updates)=v["updates"].as_array(){ui.label(format!("{} package update(s) available",updates.len()));}
                    ui.horizontal(|ui|{ui.label("Use case");egui::ComboBox::from_id_salt("profile").selected_text(if self.profile.is_empty(){"Individual packages"}else{&self.profile}).show_ui(ui,|ui|{ui.selectable_value(&mut self.profile,String::new(),"Individual packages");if let Some(profiles)=v["profiles"].as_object(){for name in profiles.keys(){ui.selectable_value(&mut self.profile,name.clone(),name);}}});});
                    ui.collapsing("Individual packages",|ui|{if let Some(packages)=v["packages"].as_object(){for (id,p) in packages{let mut selected=self.selected.contains(id);let installed=v["inventory"][id].as_str().unwrap_or("not installed");if ui.checkbox(&mut selected,format!("{} {} — {}",p["name"].as_str().unwrap_or(id),p["version"].as_str().unwrap_or(""),installed)).changed(){if selected{self.selected.insert(id.clone());}else{self.selected.remove(id);}}}}});
                    ui.checkbox(&mut self.accepted,"I have reviewed and accept the selected packages’ licenses.");
                    ui.small("Prototype: license browsing and online updates are not yet integrated. Only use approved test packages.");
                    ui.add_enabled_ui(self.accepted,|ui|{ui.horizontal(|ui|{if ui.button("Install selected / profile").clicked(){self.launch("install",&ctx)}if ui.button("Update selected (empty = all)").clicked(){self.launch("update",&ctx)}if ui.button("Remove selected").clicked(){self.launch("remove",&ctx)}});});
                }
            });
            self.dialog.update(ui);if let Some(path)=self.dialog.take_picked(){if self.pick_root{self.root=path.display().to_string()}else{self.bundle=path.display().to_string()}self.verified=None;}
            if self.rx.is_some(){ui.horizontal(|ui|{ui.spinner();ui.label("Working… verification and package operations run in the background.");});}
            if let Some(error)=&self.error{ui.colored_label(egui::Color32::from_rgb(255,130,130),format!("Unable to complete: {error}"));}
            if self.result.is_some(){ui.colored_label(egui::Color32::from_rgb(120,220,160),"Operation completed successfully.");}
            ui.separator();egui::ScrollArea::vertical().max_height(220.0).stick_to_bottom(true).show(ui,|ui|{for line in &self.logs{ui.monospace(line);}});
        });
        if self.args.ui_test_report.is_some() {
            if self.frames == 3 {
                self.capture(&ctx, "startup", frame.winit_window().is_some());
            }
            if self.start_capture.load(Ordering::SeqCst) && !self.auto_started {
                self.auto_started = true;
                if let Some(command) = self.args.ui_test_command.clone() {
                    self.launch(&command, &ctx)
                }
            }
            if self.auto_started && self.rx.is_none() && !self.final_capture {
                self.final_capture = true;
                self.capture(&ctx, "final", frame.winit_window().is_some());
            }
            ctx.request_repaint_after(std::time::Duration::from_millis(30));
        }
        // Avoid terminating a private worker midway through an operation by closing its UI.
        if self.rx.is_some() && ctx.input(|i| i.viewport().close_requested()) {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
        }
    }
}
pub fn run(args: Args) -> eframe::Result {
    eframe::run_native(
        "WPILib Manager",
        eframe::NativeOptions {
            #[cfg(target_os = "windows")]
            renderer: eframe::Renderer::Wgpu,
            #[cfg(not(target_os = "windows"))]
            renderer: eframe::Renderer::Glow,
            viewport: egui::ViewportBuilder::default().with_inner_size([940.0, 720.0]),
            ..Default::default()
        },
        Box::new(move |_cc| Ok(Box::new(App::new(args)))),
    )
}
