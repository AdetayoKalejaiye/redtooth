import RedtoothCore
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Share sheet → Redtooth → pick a person → sent directly from the extension (no app launch).
/// Uses the people list the app caches in the App Group; the extension can't wait on Contacts or a slow lookup.
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        Task { @MainActor in
            let url = await loadItem()
            let host = UIHostingController(rootView: ShareSheet(file: url) { [weak self] in
                self?.extensionContext?.completeRequest(returningItems: nil)
            })
            addChild(host)
            host.view.frame = view.bounds
            host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(host.view)
            host.didMove(toParent: self)
        }
    }

    private func loadItem() async -> URL? {
        guard let item = (extensionContext?.inputItems as? [NSExtensionItem])?.first,
              let provider = item.attachments?.first else { return nil }
        let type = provider.registeredTypeIdentifiers.first ?? UTType.data.identifier
        return await withCheckedContinuation { cont in
            _ = provider.loadFileRepresentation(forTypeIdentifier: type) { url, _ in
                // The provided URL is deleted when this callback returns; copy it into the App Group.
                guard let url else { cont.resume(returning: nil); return }
                let dst = Shared.container.appendingPathComponent("outbox", isDirectory: true)
                try? FileManager.default.createDirectory(at: dst, withIntermediateDirectories: true)
                let out = dst.appendingPathComponent(url.lastPathComponent)
                try? FileManager.default.removeItem(at: out)
                try? FileManager.default.copyItem(at: url, to: out)
                cont.resume(returning: out)
            }
        }
    }
}

struct ShareSheet: View {
    let file: URL?
    let done: () -> Void
    @State private var status: String?
    @State private var progress = 0.0
    private let people = Shared.loadPeople()

    var body: some View {
        NavigationStack {
            List {
                if let file { Label(file.lastPathComponent, systemImage: "doc") } else { Text("Nothing to send.") }
                Section("Send to") {
                    if people.isEmpty { Text("Open Redtooth once so it can find which of your contacts use it.").foregroundStyle(.secondary) }
                    ForEach(people, id: \.id) { p in
                        Button { send(to: p) } label: {
                            VStack(alignment: .leading) {
                                Text(p.name)
                                Text(p.devices.filter { !$0.endpoints.isEmpty }.map(\.label).joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .disabled(file == nil || status != nil)
                    }
                }
                if let status { Section { Text(status); ProgressView(value: progress) } }
            }
            .navigationTitle("Redtooth")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button(status == nil ? "Cancel" : "Done", action: done) }
        }
    }

    private func send(to p: (id: String, name: String, phone: String, devices: [Device])) {
        guard let file else { return }
        let order = ["phone": 0, "laptop": 1, "tablet": 2, "desktop": 3]
        guard let dest = p.devices.filter({ !$0.endpoints.isEmpty }).sorted(by: { (order[$0.kind] ?? 9) < (order[$1.kind] ?? 9) }).first else {
            status = "None of \(p.name)'s devices are reachable."; return
        }
        let first = String(p.name.split(separator: " ").first ?? "")
        let sender = Sender(meName: Shared.myName ?? "Someone", mePhone: Shared.myPhone ?? "") { t in
            Task { @MainActor in status = t.label; progress = t.progress }
        }
        status = "Waiting for \(first)"
        Task {
            let t = sender.start(FileSource(url: file), recipientFirst: first, device: dest)
            await sender.wait(t.id)
            try? FileManager.default.removeItem(at: file)
            if t.status == "saved" { try? await Task.sleep(for: .seconds(1)); done() }
        }
    }
}
