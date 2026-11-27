import RedtoothCore
import SwiftUI

/// Phone number → code → done. The number is the profile key friends' phones look up (hashed).
struct OnboardingView: View {
    @Environment(AppModel.self) private var model
    @State private var name = ""
    @State private var phone = ""
    @State private var code = ""
    @State private var sent = false
    @State private var kind: AppModel.CodeKind = .none
    @State private var error: String?
    @State private var server = Shared.directoryURL
    @State private var country = Shared.country

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Redtooth sends files and finds headphones by asking in plain words. Friends find you by your phone number.")
                        .foregroundStyle(.secondary)
                }
                Section("You") {
                    TextField("Name", text: $name).textContentType(.name)
                    Picker("Country", selection: $country) {
                        ForEach(countries.sorted { $0.name < $1.name }, id: \.iso) { c in
                            Text("\(Phones.flag(c.iso)) \(c.name) (+\(c.cc))").tag(c.iso)
                        }
                    }
                    .pickerStyle(.navigationLink)
                    LabeledContent {
                        TextField("Phone number", text: $phone).keyboardType(.phonePad).textContentType(.telephoneNumber)
                    } label: {
                        if !phone.hasPrefix("+") { Text("+\(Phones.byISO[country]?.cc ?? "1")").foregroundStyle(.secondary) }
                    }
                    Text("Contacts saved without a country code are read as \(Phones.byISO[country]?.name ?? country).")
                        .font(.footnote).foregroundStyle(.secondary)
                    if sent && kind == .link {
                        Text("This number is already set up on another device. There, open Your devices › Link another device (or run `redtooth link` on a computer) and enter the code.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    if sent { TextField(kind == .link ? "Link code" : "Code from the text message", text: $code).keyboardType(.numberPad).textContentType(.oneTimeCode) }
                }
                if let error { Text(error).foregroundStyle(.red) }
                Section {
                    Button(sent ? "Verify" : "Continue") { Task { await next() } }
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
            if !sent {
                kind = try await model.beginSetup(phone: phone, country: country, name: name)
                sent = kind != .none
            }
            else { try await model.finishVerification(phone: phone, code: code, name: name) }
        } catch { self.error = "That didn't work: \(error.localizedDescription)" }
    }
}
