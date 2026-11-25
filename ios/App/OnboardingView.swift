import RedtoothCore
import SwiftUI

/// Phone number → code → done. The number is the profile key friends' phones look up (hashed).
struct OnboardingView: View {
    @Environment(AppModel.self) private var model
    @State private var name = ""
    @State private var phone = ""
    @State private var code = ""
    @State private var sent = false
    @State private var error: String?
    @State private var server = Shared.directoryURL

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Redtooth sends files and finds headphones by asking in plain words. Friends find you by your phone number.")
                        .foregroundStyle(.secondary)
                }
                Section("You") {
                    TextField("Name", text: $name).textContentType(.name)
                    TextField("Phone number", text: $phone).keyboardType(.phonePad).textContentType(.telephoneNumber)
                    if sent { TextField("Code from the text message", text: $code).keyboardType(.numberPad).textContentType(.oneTimeCode) }
                }
                if let error { Text(error).foregroundStyle(.red) }
                Section {
                    Button(sent ? "Verify" : "Send code") { Task { await next() } }
                        .disabled(name.isEmpty || phone.count < 7 || (sent && code.count < 4))
                }
                Section("Directory server") {
                    TextField("URL", text: $server).textInputAutocapitalization(.never).keyboardType(.URL)
                        .onSubmit { model.setDirectory(server) }
                }
            }
            .navigationTitle("Welcome")
        }
    }

    private func next() async {
        error = nil
        model.setDirectory(server)
        do {
            if !sent { try await model.startVerification(phone: phone); sent = true }
            else { try await model.finishVerification(phone: phone, code: code, name: name) }
        } catch { self.error = "That didn't work: \(error.localizedDescription)" }
    }
}
