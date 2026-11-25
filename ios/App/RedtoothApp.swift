import AppIntents
import RedtoothCore
import SwiftUI
import UIKit

@main
struct RedtoothApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @Environment(\.scenePhase) private var phase
    @State private var model = AppModel.shared

    var body: some Scene {
        WindowGroup {
            Group {
                if model.onboarded { PanelView() } else { OnboardingView() }
            }
            .environment(model)
            .task { await model.start() }
            .onOpenURL { url in
                // redtooth://ask?text=…  (used by the Control Center control and Shortcuts)
                if let t = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "text" })?.value {
                    Task { await model.ask(t) }
                }
            }
        }
        .onChange(of: phase) { _, p in if p == .active { model.foregrounded() } }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ app: UIApplication, didFinishLaunchingWithOptions _: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        app.registerForRemoteNotifications()
        return true
    }

    /// Kept in the device record so a sender's directory can push “Daniel wants to send you a file”
    /// when the app isn't running (the APNs relay is a planned spike; see ios/README.md).
    func application(_: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Shared.defaults.set(token.map { String(format: "%02x", $0) }.joined(), forKey: "apns")
    }

    func application(_: UIApplication, didReceiveRemoteNotification info: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        // An offer push wakes the app briefly: start listening so the sender can connect.
        await MainActor.run { AppModel.shared.foregrounded() }
        return .newData
    }
}
