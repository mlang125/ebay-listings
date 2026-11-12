import Foundation
import UIKit

/// Photos wait here until the server has them, so a dropped connection never loses one. Each keeps
/// the ID it was given when taken; sending it again is harmless (the server already has it).
@MainActor
final class UploadQueue: ObservableObject {
    static let shared = UploadQueue()

    struct Pending: Codable, Identifiable, Hashable {
        var id: String { clientId }
        let batchId: Int
        let sku: String
        let clientId: String
    }

    @Published private(set) var pending: [Pending] = []
    @Published private(set) var problem: String?
    @Published private(set) var uploaded = 0  // goes up after each upload, so screens can refresh
    private var running = false
    private let folder: URL

    private init() {
        folder = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("uploads")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        if let data = try? Data(contentsOf: manifest), let saved = try? JSONDecoder().decode([Pending].self, from: data) { pending = saved }
        work()
    }

    private var manifest: URL { folder.appendingPathComponent("queue.json") }
    func file(_ clientId: String) -> URL { folder.appendingPathComponent(clientId + ".jpg") }
    func image(_ clientId: String) -> UIImage? { UIImage(contentsOfFile: file(clientId).path) }
    func pending(for sku: String) -> [Pending] { pending.filter { $0.sku == sku } }

    private func save() { try? JSONEncoder().encode(pending).write(to: manifest, options: .atomic) }

    func add(batchId: Int, sku: String, jpeg: Data) {
        let item = Pending(batchId: batchId, sku: sku, clientId: UUID().uuidString.lowercased())
        do { try jpeg.write(to: file(item.clientId), options: .atomic) } catch { problem = "Couldn't save the photo: \(error.localizedDescription)"; return }
        pending.append(item)
        save()
        work()
    }

    /// Forget a photo that hasn't been sent (it was deleted, or its item was).
    func drop(_ clientId: String) {
        pending.removeAll { $0.clientId == clientId }
        try? FileManager.default.removeItem(at: file(clientId))
        save()
    }

    func work() {
        guard !running else { return }
        running = true
        Task {
            var wait: UInt64 = 2
            while let next = pending.first {
                do {
                    let data = try Data(contentsOf: file(next.clientId))
                    _ = try await API.shared.request("PUT", "/batches/\(next.batchId)/items/\(API.part(next.sku))/photos/\(next.clientId)",
                                                     data: data, contentType: "image/jpeg")
                    drop(next.clientId)
                    uploaded += 1
                    problem = nil
                    wait = 2
                } catch let e as APIError where e.status == 400 || e.status == 404 {
                    problem = "A photo for \(next.sku) was refused: \(e.message)"  // won't get better by retrying
                    drop(next.clientId)
                } catch {
                    problem = "\(pending.count) photo\(pending.count == 1 ? "" : "s") waiting to upload: \((error as? APIError)?.message ?? error.localizedDescription)"
                    try? await Task.sleep(nanoseconds: wait * 1_000_000_000)
                    wait = min(wait * 2, 60)
                }
            }
            running = false
        }
    }
}
