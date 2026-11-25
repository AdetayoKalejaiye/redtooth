import AppIntents
import SwiftUI
import WidgetKit

/// Control Center / Lock Screen / Action button control (iOS 18+): opens the Redtooth panel ready to type.
@main
struct RedtoothControlsBundle: WidgetBundle {
    var body: some Widget { RedtoothControl() }
}

struct RedtoothControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "app.redtooth.control") {
            ControlWidgetButton(action: OpenRedtoothIntent()) {
                Label("Redtooth", systemImage: "dot.radiowaves.left.and.right")
            }
        }
        .displayName("Redtooth")
        .description("Ask Redtooth to send a file or connect headphones.")
    }
}

struct OpenRedtoothIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Redtooth"
    static let openAppWhenRun = true
    func perform() async throws -> some IntentResult & OpensIntent {
        .result(opensIntent: OpenURLIntent(URL(string: "redtooth://panel")!))
    }
}
