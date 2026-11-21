import AppKit
import SwiftUI

/// "On this Mac": pick an example model and download it, search Hugging Face or paste a link, manage downloads.
struct LocalModelsPage: View {
    let back: (() -> Void)?
    @Environment(\.dismiss) private var dismiss
    @Environment(ConfigStore.self) private var config
    @Environment(ProviderStatus.self) private var status
    @Environment(Installer.self) private var installer
    @State private var selected = ""
    @State private var query = ""
    @State private var removing: LocalModels.Model?
    private var local: LocalModels { .shared }

    var body: some View {
        AddProviderPage(title: "On this Mac", lead: "Free open models. Private, and they work offline.", artHeight: 92) {
            AddProviderHero(id: "local", kind: .local)
        } badge: {
            if !local.readyModels.isEmpty { SettingsBadge(text: "Ready") }
        } content: {
            if !local.loaded {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity, minHeight: 120)
            } else if !local.supported {
                Label("Needs a Mac with Apple silicon (M1 or newer).", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 80, alignment: .leading)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        examples
                        search
                        if !local.installed.isEmpty { downloaded }
                    }
                    .padding(.vertical, 2)
                }
                .scrollContentBackground(.hidden)
                .scrollEdgeEffectStyle(.soft, for: .all)
                .frame(maxHeight: 400)
                .fixedSize(horizontal: false, vertical: true)
            }
        } footer: {
            if let back {
                Button(action: back) { Label("Back", systemImage: "chevron.left") }
            }
            if local.loaded && local.supported {
                Text("\(LocalModels.size(local.bytesUsed)) used · \(LocalModels.size(local.freeBytes)) free")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
                    .padding(.leading, back == nil ? 4 : 6)
            }
            Spacer()
            if let folder = local.folder, FileManager.default.fileExists(atPath: folder.path) {
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([folder]) }
            }
            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
        .task {
            await local.refresh(installer: installer)
            if selected.isEmpty { selected = local.examples.first { !$0.installed }?.id ?? local.examples.first?.id ?? "" }
            #if DEBUG
            // `-debugLocalSearch "klein"`, `-debugLocalDownload z-image-turbo` (testing and screenshots).
            if let q = UserDefaults.standard.string(forKey: "debugLocalSearch") { query = q; runSearch() }
            if let id = UserDefaults.standard.string(forKey: "debugLocalDownload") {
                selected = id
                local.install(id, key: id, installer: installer) { downloadedOne() }
            }
            #endif
        }
        .alert("Remove \(removing?.name ?? "model")?",
               isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
               presenting: removing) { m in
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) { remove(m) }
        } message: { m in
            Text("Frees about \(LocalModels.size(m.bytes)). You can download it again anytime.")
        }
    }

    // MARK: Examples

    private var examples: some View {
        let e = local.examples.first { $0.id == selected }
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Picker("Model", selection: $selected) {
                    ForEach(local.examples) { m in
                        Text(m.installed ? "\(m.name) · Downloaded" : "\(m.name) · \(LocalModels.size(m.download))").tag(m.id)
                    }
                }
                .labelsHidden()
                .fixedSize()
                Spacer(minLength: 8)
                if let e { action(key: e.id, spec: e.id, installed: e.installed, label: "Download") }
            }
            if let e {
                HStack(spacing: 6) {
                    Text(e.note)
                    if e.edit { Pill(text: "Edits") }
                    Text(e.license).foregroundStyle(.tertiary)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if local.ramGB > 0 && e.ram > local.ramGB {
                    Label("Needs about \(e.ram) GB of memory. This Mac has \(local.ramGB) GB.", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                jobStatus(e.id)
            }
        }
    }

    // MARK: Search

    private var search: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("More models").font(.system(size: 13, weight: .semibold))
            HStack(spacing: 8) {
                TextField("Search Hugging Face or paste a link", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(runSearch)
                Button(LocalModels.isLink(query) ? "Add" : "Search", action: runSearch)
                    .disabled(query.trimmingCharacters(in: .whitespaces).isEmpty && local.lastQuery != nil)
            }
            if LocalModels.isLink(query) {
                linkRow
            } else if local.searching {
                ProgressView().controlSize(.small).padding(.vertical, 6)
            } else if let err = local.searchError {
                Text(err).font(.caption).foregroundStyle(.red)
            } else if local.lastQuery != nil {
                if local.results.isEmpty {
                    Text("Nothing found.").font(.caption).foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 0) {
                        ForEach(Array(local.results.enumerated()), id: \.element.id) { i, r in
                            if i > 0 { Divider().opacity(0.5) }
                            resultRow(r)
                        }
                    }
                    .background(Theme.well, in: RoundedRectangle(cornerRadius: 10))
                }
            }
        }
    }

    private var linkRow: some View {
        let link = query.trimmingCharacters(in: .whitespaces)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: "link").foregroundStyle(.secondary)
                Text(link).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 8)
                action(key: link, spec: link, installed: false, label: "Download")
            }
            .font(.callout)
            jobStatus(link)
        }
        .padding(10)
        .background(Theme.well, in: RoundedRectangle(cornerRadius: 10))
    }

    private func resultRow(_ r: LocalModels.SearchResult) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(r.name).font(.callout.weight(.medium)).lineLimit(1)
                        if r.edit { Pill(text: "Edits") }
                        if r.noncommercial {
                            Pill(text: "Non-commercial").help("Its license (\(r.license ?? "custom")) doesn't allow commercial use")
                        }
                    }
                    Text(details(r)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                if !r.supported {
                    Text("Not supported").font(.caption).foregroundStyle(.tertiary).help(r.reason ?? "")
                } else if r.gated {
                    Text("Needs login").font(.caption).foregroundStyle(.tertiary)
                        .help("Accept the model's terms on huggingface.co, then run: imagegenkit set-key huggingface")
                } else {
                    action(key: r.repo, spec: r.repo, installed: r.installed, label: "Get")
                }
            }
            jobStatus(r.repo)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .contentShape(Rectangle())
        .contextMenu {
            Button("Open on Hugging Face") { Actions.open(URL(string: "https://huggingface.co/\(r.repo)")!) }
        }
    }

    private func details(_ r: LocalModels.SearchResult) -> String {
        var parts = [r.author, "↓ \(LocalModels.count(r.downloads))"]
        if let f = r.family { parts.append(f) }
        if let size = r.size { parts.append(LocalModels.size(size + (r.extra ?? 0))) }
        if let l = r.license, !l.isEmpty { parts.append(l) }
        return parts.joined(separator: " · ")
    }

    // MARK: Downloaded

    private var downloaded: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Downloaded").font(.system(size: 13, weight: .semibold))
            VStack(spacing: 0) {
                ForEach(Array(local.installed.enumerated()), id: \.element.id) { i, m in
                    if i > 0 { Divider().opacity(0.5) }
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(m.name).font(.callout.weight(.medium)).lineLimit(1)
                                if m.edit { Pill(text: "Edits") }
                            }
                            Text(m.ready ? [m.family == m.name ? "" : m.family, LocalModels.size(m.bytes)].filter { !$0.isEmpty }.joined(separator: " · ")
                                 : "Files missing. Download it again.")
                                .font(.caption)
                                .foregroundStyle(m.ready ? Color.secondary : .orange)
                        }
                        Spacer(minLength: 8)
                        Button(role: .destructive) { removing = m } label: {
                            Image(systemName: "trash").foregroundStyle(.red)
                        }
                        .buttonStyle(.borderless)
                        .help("Remove")
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                }
            }
            .background(Theme.well, in: RoundedRectangle(cornerRadius: 10))
        }
    }

    // MARK: Shared pieces

    /// Download / Cancel / Downloaded for one model.
    @ViewBuilder
    private func action(key: String, spec: String, installed: Bool, label: String) -> some View {
        if case .downloading = local.jobs[key] {
            Button("Cancel") { local.cancel(key) }
        } else if installed {
            SettingsBadge(text: "Downloaded")
        } else {
            Button(label) {
                local.clearFailure(key)
                local.install(spec, key: key, installer: installer) { downloadedOne() }
            }
            .buttonStyle(.borderedProminent)
        }
    }

    @ViewBuilder
    private func jobStatus(_ key: String) -> some View {
        switch local.jobs[key] {
        case .downloading(let done, let total, let file):
            VStack(alignment: .leading, spacing: 4) {
                ProgressView(value: total > 0 ? Double(done) : 0, total: Double(max(total, 1)))
                Text(total > 0 ? "\(LocalModels.size(done)) of \(LocalModels.size(total))" + (file == "engine" ? " · Engine" : "")
                     : "Starting…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        case .failed(let message):
            Text(message).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
        case nil:
            EmptyView()
        }
    }

    private func runSearch() {
        let q = query.trimmingCharacters(in: .whitespaces)
        if LocalModels.isLink(q) {
            local.clearFailure(q)
            local.install(q, key: q, installer: installer) { downloadedOne() }
        } else {
            Task { await local.search(q, installer: installer) }
        }
    }

    /// A download finished: use On this Mac for images and refresh its status.
    private func downloadedOne() {
        addProviderEnable("local", before: config.enabled(ready: { status.state($0).isReady }).map(\.id), config: config)
        Task { await status.refresh(config.providers) }
    }

    private func remove(_ m: LocalModels.Model) {
        Task {
            _ = await local.remove(m.id, installer: installer)
            await status.refresh(config.providers)
        }
    }
}
