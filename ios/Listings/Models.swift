import Foundation

// The JSON the server sends (docs/API.md). Keys arrive in snake_case and are decoded as camelCase.
// Unknown fields are ignored, so the server can add fields without breaking the app.

struct User: Codable, Equatable { let id: Int; let name: String; let role: String }
struct TokenReply: Codable { let token: String; let user: User }
struct MeReply: Codable { let user: User }

struct Item: Codable, Identifiable, Hashable {
    var id: String { sku }
    let sku: String
    let title: String?
    let status: String
    let tote: String?
    let price: Double?
    let quantity: Int?
    let notes: String?
    let missingSince: String?
    let daysListed: Int?
    let suggestedPrice: Double?
}
struct ItemsReply: Codable { let items: [Item]; let total: Int }
struct ItemReply: Codable { let item: Item }

struct Tote: Codable, Identifiable, Hashable {
    var id: String { code }
    let code: String
    let kind: String
    let checkedAt: String?
    let onHand: Int?
    let listed: Int?
    let missing: Int?
    let value: Double?
}
struct TotesReply: Codable { let totes: [Tote]; let nextFree: String }
struct ToteReply: Codable { let tote: Tote; let items: [Item] }

struct Note: Codable, Equatable { var tags: [String]; var text: String }
struct Photo: Codable, Hashable { let file: String; let url: String }
struct DraftSummary: Codable, Hashable { let title: String?; let price: Double?; let condition: String?; let needsCheck: [String]? }

struct CaptureItem: Codable, Identifiable, Equatable {
    var id: String { sku }
    let sku: String
    let position: Int
    var photos: [Photo]
    var note: Note
    let drafted: Bool
    let draft: DraftSummary?
    static func == (a: CaptureItem, b: CaptureItem) -> Bool { a.sku == b.sku && a.photos == b.photos && a.note == b.note && a.drafted == b.drafted }
}
struct Batch: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let tote: String?
    let createdAt: String
    let finishedAt: String?
    var items: [CaptureItem]
}
struct BatchReply: Codable { let batch: Batch }
struct BatchesReply: Codable { let batches: [Batch] }
struct NewItemReply: Codable { let sku: String; let batch: Batch }
struct PhotoReply: Codable { let file: String; let added: Bool }
struct NoteTags: Codable { let recent: [String]; let standard: [String] }

struct JobError: Codable, Hashable { let sku: String?; let error: String }
struct Job: Codable, Identifiable {
    let id: Int
    let status: String
    let done: Int
    let total: Int
    let errors: [JobError]
    let costUsd: Double
    var finished: Bool { ["done", "failed", "interrupted"].contains(status) }
}
struct JobReply: Codable { let job: Job }
struct JobStartReply: Codable { let job: Job; let estimateUsd: Double; let noPhotos: [String] }

/// A filter value from the server: a string or a number.
struct FilterValue: Codable, Hashable {
    let text: String
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { text = s } else if let i = try? c.decode(Int.self) { text = String(i) }
        else { text = String(try c.decode(Double.self)) }
    }
    func encode(to encoder: Encoder) throws { var c = encoder.singleValueContainer(); try c.encode(text) }
}
struct Attention: Codable, Identifiable, Hashable {
    var id: String { key }
    let key: String
    let count: Int
    let what: String
    let list: String
    let filters: [String: FilterValue]
}
struct TodayCount: Codable, Hashable { let label: String; let count: Int }
struct HomeReply: Codable {
    let attention: [Attention]
    let counts: [String: Int]
    let toShip: Int
    let overdue: [String]
    let dueToday: [String]
    let today: [TodayCount]
    let lastSync: String?
}
struct PickItem: Codable, Identifiable, Hashable {
    var id: String { sku }
    let sku: String
    let status: String
    let tote: String?
    let shipBy: String?
    let title: String?
    let missingSince: String?
}
struct PickListReply: Codable { let items: [PickItem]; let total: Int; let lastSync: String? }
struct PackReply: Codable { let changed: [String]; let skipped: [String] }
struct DoneReply: Codable { let done: String }
struct SyncReply: Codable { let orders: Int; let newSales: Int }
struct CountReply: Codable { let message: String }
