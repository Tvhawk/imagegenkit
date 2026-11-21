import Foundation
import Observation

/// Image models that run on this Mac (stable-diffusion.cpp), managed through the CLI's `local-models` commands,
/// so the app and `imagegenkit` share one list. Downloads stream their progress; cancelling keeps the partial file,
/// and the next download of the same model continues where it stopped.
@Observable
final class LocalModels {
    static let shared = LocalModels()

    struct Model: Identifiable, Hashable {
        let id: String
        let name: String
        let family: String
        let edit: Bool
        let bytes: Int64
        let license: String?
        let source: String?
        let ready: Bool
    }

    /// A curated model from catalog/local.json (the dropdown).
    struct Example: Identifiable, Hashable {
        let id: String
        let name: String
        let note: String
        let license: String
        let ram: Int
        let download: Int64
        let edit: Bool
        let installed: Bool
    }

    struct SearchResult: Identifiable, Hashable {
        var id: String { repo }
        let repo: String
        let author: String
        let name: String
        let downloads: Int
        let license: String?
        let gated: Bool
        let family: String?
        let edit: Bool
        let size: Int64?
        let extra: Int64?
        let supported: Bool
        let reason: String?
        let installed: Bool
    }

    enum Job: Equatable {
        case downloading(done: Int64, total: Int64, file: String)
        case failed(String)
    }

    private(set) var loaded = false
    private(set) var supported = true
    private(set) var ramGB = 0
    private(set) var installed: [Model] = []
    private(set) var examples: [Example] = []
    private(set) var bytesUsed: Int64 = 0
    private(set) var freeBytes: Int64 = 0
    private(set) var folder: URL?
    private(set) var jobs: [String: Job] = [:]
    private(set) var results: [SearchResult] = []
    private(set) var searching = false
    private(set) var searchError: String?
    private(set) var lastQuery: String?
    @ObservationIgnored private var tasks: [String: Task<Void, Never>] = [:]

    var readyModels: [Model] { installed.filter(\.ready) }

    // MARK: Status without the CLI (fast, for provider status)

    nonisolated static let home = Paths.home.appending(path: ".local/share/imagegenkit")

    /// Downloaded models whose files are all there, read straight from the registry the CLI writes.
    nonisolated static func readyCount() -> Int {
        let fm = FileManager.default
        guard let data = try? Data(contentsOf: home.appending(path: "local-models.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return 0 }
        guard let engine = obj["engine"] as? [String: Any], let version = engine["version"] as? String,
              fm.isExecutableFile(atPath: home.appending(path: "engine/\(version)/\(engine["binary"] as? String ?? "sd-cli")").path)
        else { return 0 }
        let models = obj["models"] as? [[String: Any]] ?? []
        return models.filter { m in
            (m["files"] as? [String: String] ?? [:]).values.allSatisfy { rel in
                let url = rel.hasPrefix("/") ? URL(fileURLWithPath: rel) : home.appending(path: "models/\(rel)")
                return fm.fileExists(atPath: url.path)
            }
        }.count
    }

    nonisolated static var appleSilicon: Bool {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname("hw.optional.arm64", &value, &size, nil, 0) == 0 && value == 1
    }

    // MARK: Loading

    func refresh(installer: Installer, reloadCatalog: Bool = false) async {
        let r = await installer.runCLI(["local-models", "list", "--json"] + (reloadCatalog ? ["--refresh"] : []))
        guard r.status == 0, let obj = try? JSONSerialization.jsonObject(with: Data(r.stdout.utf8)) as? [String: Any] else {
            loaded = true
            supported = Self.appleSilicon
            return
        }
        supported = obj["supported"] as? Bool ?? false
        ramGB = obj["ram_gb"] as? Int ?? 0
        bytesUsed = (obj["bytes"] as? NSNumber)?.int64Value ?? 0
        freeBytes = (obj["free"] as? NSNumber)?.int64Value ?? 0
        folder = (obj["dir"] as? String).map { URL(fileURLWithPath: $0) }
        installed = (obj["models"] as? [[String: Any]] ?? []).map { m in
            Model(id: m["id"] as? String ?? "", name: m["name"] as? String ?? "", family: m["family"] as? String ?? "",
                  edit: m["edit"] as? Bool ?? false, bytes: (m["bytes"] as? NSNumber)?.int64Value ?? 0,
                  license: m["license"] as? String, source: m["source"] as? String, ready: m["ready"] as? Bool ?? false)
        }
        examples = (obj["catalog"] as? [[String: Any]] ?? []).map { m in
            Example(id: m["id"] as? String ?? "", name: m["name"] as? String ?? "", note: m["note"] as? String ?? "",
                    license: m["license"] as? String ?? "", ram: m["ram"] as? Int ?? 0,
                    download: (m["download"] as? NSNumber)?.int64Value ?? 0, edit: m["edit"] as? Bool ?? false,
                    installed: m["installed"] as? Bool ?? false)
        }
        loaded = true
    }

    // MARK: Download

    var isBusy: Bool { jobs.values.contains { if case .downloading = $0 { true } else { false } } }

    /// Downloads a model (a catalog id, a Hugging Face repo or link). `key` identifies the job in the UI.
    func install(_ spec: String, key: String, installer: Installer, done: @escaping @MainActor () -> Void) {
        guard tasks[key] == nil else { return }
        jobs[key] = .downloading(done: 0, total: 0, file: "")
        tasks[key] = Task {
            let r = await installer.streamCLI(["local-models", "add", spec, "--json"]) { line, isError in
                guard !isError, let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      obj["event"] as? String == "progress" || obj["event"] as? String == "start" else { return }
                let done = (obj["done"] as? NSNumber)?.int64Value ?? 0
                let total = (obj["total"] as? NSNumber)?.int64Value ?? 0
                let file = obj["file"] as? String ?? ""
                Task { @MainActor in
                    if case .downloading = LocalModels.shared.jobs[key] {
                        LocalModels.shared.jobs[key] = .downloading(done: done, total: total, file: file)
                    }
                }
            }
            tasks[key] = nil
            if Task.isCancelled {
                jobs[key] = nil
            } else if r.status == 0 {
                jobs[key] = nil
                await refresh(installer: installer)
                done()
            } else {
                jobs[key] = .failed(Self.message(r.stderr))
            }
        }
    }

    func cancel(_ key: String) {
        tasks[key]?.cancel()
        tasks[key] = nil
        jobs[key] = nil
    }

    /// Stops every download (the app is quitting); the partial files stay and the next download continues them.
    func cancelAll() {
        for key in Array(tasks.keys) { cancel(key) }
    }

    func clearFailure(_ key: String) {
        if case .failed = jobs[key] { jobs[key] = nil }
    }

    func remove(_ id: String, installer: Installer) async -> Bool {
        let r = await installer.runCLI(["local-models", "remove", id, "--json"])
        await refresh(installer: installer)
        return r.status == 0
    }

    // MARK: Search

    func search(_ query: String, installer: Installer) async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        lastQuery = q
        searching = true
        searchError = nil
        defer { if lastQuery == q { searching = false } }
        let r = await installer.runCLI(["local-models", "search", q, "--limit", "24", "--json"])
        guard lastQuery == q else { return }  // a newer search started
        guard r.status == 0, let rows = try? JSONSerialization.jsonObject(with: Data(r.stdout.utf8)) as? [[String: Any]] else {
            results = []
            searchError = Self.message(r.stderr)
            return
        }
        results = rows.map { m in
            SearchResult(repo: m["repo"] as? String ?? "", author: m["author"] as? String ?? "", name: m["name"] as? String ?? "",
                         downloads: m["downloads"] as? Int ?? 0, license: m["license"] as? String, gated: m["gated"] as? Bool ?? false,
                         family: m["family"] as? String, edit: m["edit"] as? Bool ?? false,
                         size: (m["size"] as? NSNumber)?.int64Value, extra: (m["extra"] as? NSNumber)?.int64Value,
                         supported: m["supported"] as? Bool ?? false, reason: m["reason"] as? String,
                         installed: m["installed"] as? Bool ?? false)
        }
    }

    func clearSearch() {
        lastQuery = nil
        results = []
        searchError = nil
        searching = false
    }

    // MARK: Helpers

    /// The CLI's last error line, without its prefix.
    nonisolated static func message(_ stderr: String) -> String {
        let line = stderr.split(separator: "\n").last { $0.hasPrefix("imagegenkit:") } ?? stderr.split(separator: "\n").last ?? ""
        let text = line.replacingOccurrences(of: "imagegenkit: ", with: "").trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? "Download failed." : text.prefix(1).uppercased() + text.dropFirst()
    }

    /// A Hugging Face repo or link, or another download link, rather than search words.
    nonisolated static func isLink(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("http://") || t.hasPrefix("https://") { return true }
        return t.range(of: #"^[\w.-]+/[\w.-]+$"#, options: .regularExpression) != nil
    }

    nonisolated static func size(_ bytes: Int64) -> String {
        bytes >= 1_000_000_000 ? String(format: "%.1f GB", Double(bytes) / 1e9) : String(format: "%.0f MB", Double(bytes) / 1e6)
    }

    nonisolated static func count(_ n: Int) -> String {
        n >= 1_000_000 ? String(format: "%.1fM", Double(n) / 1e6) : n >= 1000 ? String(format: "%.0fK", Double(n) / 1e3) : "\(n)"
    }
}
