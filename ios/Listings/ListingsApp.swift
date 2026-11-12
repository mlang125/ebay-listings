import SwiftUI

@main
struct ListingsApp: App {
    @StateObject private var session = Session()
    var body: some Scene {
        WindowGroup {
            Group {
                if session.user == nil { SignInView() } else { RootView() }
            }
            .environmentObject(session)
            .task { await session.restore() }
        }
    }
}

/// Who is signed in. Any 401 from the server signs the app out (password changed, account turned off).
@MainActor
final class Session: ObservableObject {
    @Published var user: User?
    @Published var checking = true

    func restore() async {
        defer { checking = false }
        #if DEBUG
        // Simulator checks: launch with -debugToken <token> -server <url> to start signed in.
        if let t = UserDefaults.standard.string(forKey: "debugToken") { API.shared.token = t }
        #endif
        guard API.shared.token != nil else { return }
        do { user = try await API.shared.get("/me", as: MeReply.self).user } catch let e as APIError where e.signedOut { signOut(local: true) } catch {}
    }

    func signIn(server: String, name: String, password: String) async throws {
        var address = server.trimmingCharacters(in: .whitespaces)
        if !address.contains("://") { address = "http://" + address }
        if address.filter({ $0 == ":" }).count == 1 { address += ":5050" }  // the server's usual port
        guard let url = URL(string: address) else { throw APIError(status: 0, code: "bad_request", message: "That isn't a server address.") }
        API.shared.server = url
        API.shared.token = nil
        let reply: TokenReply = try await API.shared.send("POST", "/auth/token",
            ["name": name, "password": password, "device": UIDevice.current.name])
        API.shared.token = reply.token
        user = reply.user
    }

    func signOut(local: Bool = false) {
        if !local { Task { try? await API.shared.sendNoReply("DELETE", "/auth/token") } }
        API.shared.token = nil
        user = nil
    }

    /// Run a server call; a 401 signs out, anything else comes back as a message to show.
    func run(_ work: () async throws -> Void) async -> String? {
        do { try await work(); return nil } catch let e as APIError {
            if e.signedOut { signOut(local: true) }
            return e.message
        } catch { return error.localizedDescription }
    }
}

struct SignInView: View {
    @EnvironmentObject var session: Session
    @State private var server = API.shared.server?.absoluteString ?? ""
    @State private var name = ""
    @State private var password = ""
    @State private var problem: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server, e.g. 192.168.1.20", text: $server)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: { Text("The address `cli.py serve --lan` prints on the Mac.") }
                Section {
                    TextField("Name", text: $name).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .textContentType(.username)
                    SecureField("Password", text: $password).textContentType(.password)
                }
                if let problem { Section { Text(problem).foregroundStyle(.red) } }
                Section {
                    Button(busy ? "Signing in…" : "Sign in") {
                        busy = true
                        Task {
                            problem = await session.run { try await session.signIn(server: server, name: name, password: password) }
                            busy = false
                        }
                    }.disabled(busy || server.isEmpty || name.isEmpty || password.isEmpty)
                }
            }
            .navigationTitle("Listings")
        }
    }
}

struct RootView: View {
    @AppStorage("tab") private var tab = "shoot"  // reopens where you were
    var body: some View {
        TabView(selection: $tab) {
            CaptureHomeView().tabItem { Label("Shoot", systemImage: "camera") }.tag("shoot")
            HomeView().tabItem { Label("Home", systemImage: "house") }.tag("home")
            ItemsView().tabItem { Label("Items", systemImage: "list.bullet") }.tag("items")
            TotesView().tabItem { Label("Totes", systemImage: "shippingbox") }.tag("totes")
            ShipView().tabItem { Label("Ship", systemImage: "paperplane") }.tag("ship")
        }
    }
}

/// A red line for a failed call, shown at the top of a list.
struct ProblemRow: View {
    let text: String?
    var body: some View { if let text { Text(text).foregroundStyle(.red).font(.callout) } }
}

func money(_ value: Double?) -> String {
    guard let value else { return "" }
    return value.formatted(.currency(code: "USD"))
}
