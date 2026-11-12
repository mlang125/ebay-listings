import Foundation

struct APIError: LocalizedError {
    let status: Int
    let code: String
    let message: String
    var errorDescription: String? { message }
    var signedOut: Bool { status == 401 }
}

private struct ErrorBody: Codable { struct Inner: Codable { let code: String; let message: String }; let error: Inner }

/// Talks to the listings server's /api/v1. One shared instance; the server address and token are
/// remembered between launches (the token in the Keychain).
final class API {
    static let shared = API()

    var server: URL? {
        get { UserDefaults.standard.string(forKey: "server").flatMap(URL.init(string:)) }
        set { UserDefaults.standard.set(newValue?.absoluteString, forKey: "server") }
    }
    var token: String? {
        get { Keychain.get("token") }
        set { Keychain.set("token", newValue) }
    }

    private let decoder: JSONDecoder = { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }()
    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 30
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    func url(_ path: String) throws -> URL {
        guard let server else { throw APIError(status: 0, code: "no_server", message: "Enter the server address first.") }
        let full = path.hasPrefix("/api/") ? path : "/api/v1" + path
        guard let u = URL(string: full, relativeTo: server) else { throw APIError(status: 0, code: "bad_request", message: "Bad address.") }
        return u.absoluteURL
    }

    func request(_ method: String, _ path: String, json: [String: Any]? = nil, data: Data? = nil, contentType: String? = nil) async throws -> Data {
        var req = URLRequest(url: try url(path))
        req.httpMethod = method
        if let token { req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if let json {
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        } else if let data {
            req.httpBody = data
            req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        let (body, response): (Data, URLResponse)
        do {
            (body, response) = try await session.data(for: req)
        } catch {
            throw APIError(status: 0, code: "offline", message: "Can't reach the server. Is it running, and is this phone on the home wifi?")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            if let e = try? decoder.decode(ErrorBody.self, from: body) {
                throw APIError(status: status, code: e.error.code, message: e.error.message)
            }
            throw APIError(status: status, code: "server_error", message: "The server answered \(status).")
        }
        return body
    }

    func get<T: Decodable>(_ path: String, as: T.Type = T.self) async throws -> T {
        try decoder.decode(T.self, from: try await request("GET", path))
    }

    func send<T: Decodable>(_ method: String, _ path: String, _ json: [String: Any] = [:], as: T.Type = T.self) async throws -> T {
        try decoder.decode(T.self, from: try await request(method, path, json: json))
    }

    func sendNoReply(_ method: String, _ path: String, _ json: [String: Any]? = nil) async throws {
        _ = try await request(method, path, json: json)
    }

    /// Path pieces (SKUs, tote codes) are plain letters, digits and dashes, but encode them anyway.
    static func part(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(["/"])) ?? s }
}
