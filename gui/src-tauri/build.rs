fn main() {
    let _ = std::fs::create_dir_all("resources/runtime");
    let _ = std::fs::create_dir_all("../dist");
    tauri_build::build();
}
