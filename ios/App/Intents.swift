import AppIntents
import RedtoothCore

/// Siri / Shortcuts / Action button: “Ask Redtooth to send Daniel the deck I edited this morning”.
struct AskRedtoothIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Redtooth"
    static let description = IntentDescription("Send a file, find a file, or connect to headphones by asking in plain words.")
    static let openAppWhenRun = true

    @Parameter(title: "Request", requestValueDialog: "What should Redtooth do?")
    var request: String

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let model = AppModel.shared
        if model.agent == nil { await model.start() }
        await model.ask(request)
        return .result(dialog: IntentDialog(stringLiteral: model.feed.last?.text ?? "Done."))
    }
}

struct SendFileIntent: AppIntent {
    static let title: LocalizedStringResource = "Send File with Redtooth"
    static let openAppWhenRun = true

    @Parameter(title: "File") var file: IntentFile
    @Parameter(title: "To") var person: String

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let model = AppModel.shared
        if model.agent == nil { await model.start() }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("shortcut-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(file.filename)
        try file.data.write(to: url)
        let v = try url.resourceValues(forKeys: [.fileSizeKey])
        model.send(FileHit(id: url.path, path: url.path, name: file.filename, kind: "file", ext: "." + url.pathExtension.lowercased(),
                           size: Int64(v.fileSize ?? 0), mtime: Date().timeIntervalSince1970))
        await model.ask("send it to \(person)")
        return .result(dialog: IntentDialog(stringLiteral: model.feed.last?.text ?? "Sending."))
    }
}

struct ConnectHeadphonesIntent: AppIntent {
    static let title: LocalizedStringResource = "Connect to Headphones"
    static let openAppWhenRun = true
    @Parameter(title: "Whose", default: "my") var owner: String

    @MainActor
    func perform() async throws -> some IntentResult {
        let model = AppModel.shared
        if model.agent == nil { await model.start() }
        await model.ask(owner.lowercased() == "my" ? "connect to my headphones" : "connect to \(owner)'s headphones")
        return .result()
    }
}

struct RedtoothShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskRedtoothIntent(), phrases: ["Ask \(.applicationName)", "Send a file with \(.applicationName)"],
                    shortTitle: "Ask Redtooth", systemImageName: "dot.radiowaves.left.and.right")
        AppShortcut(intent: ConnectHeadphonesIntent(), phrases: ["Connect headphones with \(.applicationName)"],
                    shortTitle: "Headphones", systemImageName: "headphones")
    }
}
