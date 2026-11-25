import AVKit
import RedtoothCore
import SwiftUI

/// The compact panel: one input, a short feed of replies with their choices, transfers and offers.
struct PanelView: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @FocusState private var focused: Bool
    @State private var showFolders = false

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(model.incoming, id: \.id) { IncomingCard(offer: $0) }
                        if model.feed.isEmpty { Suggestions { text = $0; submit() } }
                        ForEach(model.feed) { item in FeedRow(item: item).id(item.id) }
                        if model.busy { ProgressView().padding(.leading, 4) }
                    }
                    .padding()
                }
                .onChange(of: model.feed.count) { _, _ in withAnimation { proxy.scrollTo(model.feed.last?.id, anchor: .bottom) } }
            }
            .safeAreaInset(edge: .bottom) { inputBar }
            .navigationTitle("Redtooth")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Folders to search…", systemImage: "folder.badge.plus") { showFolders = true }
                        if DirectLink.available { Text("Wi-Fi Aware ready") }
                        MetricsMenu(metrics: model.lastMetrics)
                    } label: { Image(systemName: "ellipsis.circle") }
                }
                ToolbarItem(placement: .topBarLeading) { RoutePicker(hint: nil).frame(width: 32, height: 32) }
            }
            .sheet(isPresented: $showFolders) { FoldersView() }
            .sheet(item: Binding(get: { model.routeHint.map(RouteHint.init) }, set: { model.routeHint = $0?.name })) { RouteSheet(hint: $0.name) }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("Send Daniel the deck I edited this morning", text: $text, axis: .vertical)
                .lineLimit(1...3)
                .focused($focused)
                .submitLabel(.send)
                .onSubmit(submit)
            Button(action: submit) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
                .accessibilityLabel("Send")
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .glass(cornerRadius: 22)
        .padding(.horizontal).padding(.bottom, 6)
    }

    private func submit() {
        let t = text
        text = ""
        Task { await model.ask(t) }
    }
}

struct RouteHint: Identifiable { let name: String; var id: String { name } }

/// iOS doesn't let apps switch Bluetooth audio; the system route picker does, with the device named for the user.
struct RouteSheet: View {
    let hint: String
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(spacing: 16) {
            Text("Choose **\(hint)**").font(.title3)
            RoutePicker(hint: hint).frame(width: 64, height: 64)
            Text("Tap the audio button, then pick \(hint). If it isn't listed, pair it first in Settings › Bluetooth (it must be in pairing mode).")
                .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
            HStack {
                Button("Bluetooth Settings") { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) }
                Spacer()
                Button("Done") { dismiss() }.bold()
            }
        }
        .padding(24)
        .presentationDetents([.height(280)])
    }
}

struct RoutePicker: UIViewRepresentable {
    let hint: String?
    func makeUIView(context: Context) -> AVRoutePickerView {
        let v = AVRoutePickerView()
        v.prioritizesVideoDevices = false
        v.accessibilityLabel = hint.map { "Choose \($0)" } ?? "Audio output"
        return v
    }
    func updateUIView(_ v: AVRoutePickerView, context: Context) {}
}

struct FeedRow: View {
    @Environment(AppModel.self) private var model
    let item: FeedItem

    var body: some View {
        switch item.who {
        case .user:
            HStack { Spacer(minLength: 40); Text(item.text).padding(10).background(.tint.opacity(0.15), in: .rect(cornerRadius: 14)) }
        case .agent:
            VStack(alignment: .leading, spacing: 8) {
                if let s = item.reply?.status { Text(s.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary) }
                Text(item.text).foregroundStyle(item.reply?.ok == false ? .secondary : .primary)
                if let q = item.reply?.question, model.agent?.ctx.pending?.id == q.id {
                    ForEach(q.options, id: \.id) { o in
                        Button { Task { await model.choose(q, o) } } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(o.label).font(.body.weight(.medium))
                                if !o.detail.isEmpty { Text(o.detail).font(.caption).foregroundStyle(.secondary) }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.bordered)
                    }
                } else if let files = item.reply?.files, !files.isEmpty, item.reply?.transferId == nil {
                    ForEach(files, id: \.id) { f in
                        Button { model.send(f) } label: { FileLine(hit: f) }.buttonStyle(.plain)
                    }
                }
                if let id = item.reply?.transferId { TransferLine(id: id) }
                if case let .showRoutePicker(hint) = item.reply?.action ?? .none {
                    Button("Choose \(hint)", systemImage: "airplayaudio") { model.routeHint = hint }.buttonStyle(.borderedProminent)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glass(cornerRadius: 16)
        }
    }
}

struct FileLine: View {
    let hit: FileHit
    var body: some View {
        HStack {
            Image(systemName: Self.icon(hit.ext)).frame(width: 24)
            VStack(alignment: .leading) {
                Text(hit.name).lineLimit(1)
                Text("Edited \(Agent.whenLabel(hit.mtime))").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    static func icon(_ ext: String) -> String {
        switch ext {
        case ".pptx", ".key": "rectangle.on.rectangle"
        case ".pdf": "doc.richtext"
        case ".xlsx", ".csv", ".numbers": "tablecells"
        case ".jpg", ".jpeg", ".png", ".heic": "photo"
        case ".mov", ".mp4": "film"
        default: "doc"
        }
    }
}

struct TransferLine: View {
    @Environment(AppModel.self) private var model
    let id: String
    var body: some View {
        if let t = model.transfers[id] {
            VStack(alignment: .leading, spacing: 4) {
                ProgressView(value: t.progress)
                HStack {
                    Text(t.label).font(.caption)
                    Spacer()
                    if !["saved", "declined", "failed", "cancelled"].contains(t.status) {
                        Button("Cancel") { model.agent?.sender.cancel(id) }.font(.caption)
                    }
                }
            }
        }
    }
}

struct IncomingCard: View {
    @Environment(AppModel.self) private var model
    let offer: IncomingOffer
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(offer.senderName) wants to send").font(.caption).foregroundStyle(.secondary)
            Text(offer.name).font(.headline)
            switch offer.status {
            case "waiting":
                HStack {
                    Button("Decline", role: .destructive) { model.decide(offer.id, accept: false) }
                    Spacer()
                    Button("Accept") { model.decide(offer.id, accept: true) }.buttonStyle(.borderedProminent)
                }
            case "receiving": ProgressView(value: Double(offer.received), total: Double(max(offer.size, 1)))
            case "saved": Label("Saved to Files › Redtooth", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            default: Text(offer.status.capitalized).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .glass(cornerRadius: 16)
    }
}

struct Suggestions: View {
    let pick: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(["Send Daniel the deck I edited this morning", "Find the deck about customer churn", "Connect to Daniel's headphones"], id: \.self) { s in
                Button(s) { pick(s) }.buttonStyle(.bordered)
            }
        }
    }
}

struct MetricsMenu: View {
    let metrics: [String: Double]
    var body: some View {
        Section("Last request") {
            ForEach(metrics.sorted { $0.key < $1.key }, id: \.key) { k, v in Text("\(k.replacingOccurrences(of: "_", with: " ")): \(Int(v)) ms") }
        }
    }
}

struct FoldersView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var picking = false
    @State private var folders: [(id: String, name: String, url: URL?)] = []

    var body: some View {
        NavigationStack {
            List {
                Section(footer: Text("iOS lets Redtooth search only folders you choose here. Files you share into Redtooth are searchable too.")) {
                    ForEach(folders, id: \.id) { f in Label(f.name, systemImage: f.url == nil ? "exclamationmark.triangle" : "folder") }
                    Button("Add folder…", systemImage: "plus") { picking = true }
                }
                if let p = model.files.indexing(), !p.complete { Text("Indexing \(p.done) of \(p.queued)…").foregroundStyle(.secondary) }
            }
            .navigationTitle("Folders")
            .toolbar { Button("Done") { dismiss() } }
            .fileImporter(isPresented: $picking, allowedContentTypes: [.folder]) { r in
                if case let .success(url) = r { try? model.files.addFolder(url); folders = model.files.folders(); Task { await model.files.refresh() } }
            }
            .onAppear { folders = model.files.folders() }
        }
    }
}

extension View {
    /// Liquid Glass on iOS 26, material elsewhere.
    @ViewBuilder func glass(cornerRadius: CGFloat) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular, in: .rect(cornerRadius: cornerRadius))
        } else {
            self.background(.ultraThinMaterial, in: .rect(cornerRadius: cornerRadius))
        }
    }
}
