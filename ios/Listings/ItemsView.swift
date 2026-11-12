import SwiftUI

let statuses = ["PENDING", "LISTED", "BOUGHT", "LABEL_PRINTED", "PACKAGED", "SENT_TO_POST_OFFICE", "DELIVERED",
                "CANCELLED", "RETURNED", "ENDED", "SKIPPED"]

func label(_ status: String) -> String { status.replacingOccurrences(of: "_", with: " ").capitalized }

struct ItemsView: View {
    @State private var query = ""
    @State private var status = "ON_HAND"
    var body: some View {
        NavigationStack {
            ItemList(title: "Items", filters: ["status": status], query: query)
                .searchable(text: $query, prompt: "SKU or words in the title")
                .toolbar {
                    Picker("Status", selection: $status) {
                        Text("On hand").tag("ON_HAND"); Text("Missing").tag("MISSING"); Text("Any").tag("")
                        ForEach(statuses, id: \.self) { Text(label($0)).tag($0) }
                    }
                }
        }
    }
}

struct ItemList: View {
    @EnvironmentObject var session: Session
    let title: String
    var filters: [String: String] = [:]
    var query = ""
    @State private var items: [Item] = []
    @State private var total = 0
    @State private var problem: String?

    var body: some View {
        List {
            ProblemRow(text: problem)
            if total > items.count { Text("Showing \(items.count) of \(total)").font(.caption).foregroundStyle(.secondary) }
            ForEach(items) { item in NavigationLink { ItemDetail(sku: item.sku) } label: { ItemRow(item: item) } }
        }
        .navigationTitle(title)
        .refreshable { await load() }
        .task(id: "\(filters)\(query)") { await load() }
    }

    func load() async {
        var parts = URLComponents()
        parts.queryItems = filters.filter { !$0.value.isEmpty }.map { URLQueryItem(name: $0.key, value: $0.value) }
            + (query.isEmpty ? [] : [URLQueryItem(name: "q", value: query)]) + [URLQueryItem(name: "limit", value: "200")]
        problem = await session.run {
            let r: ItemsReply = try await API.shared.get("/items?" + (parts.percentEncodedQuery ?? ""))
            items = r.items; total = r.total
        }
    }
}

struct ItemRow: View {
    let item: Item
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(item.sku).font(.caption.monospaced())
                Text(label(item.status)).font(.caption).foregroundStyle(.secondary)
                if item.missingSince != nil { Text("missing").font(.caption).foregroundStyle(.red) }
                Spacer()
                Text(item.tote ?? "?").font(.caption.bold())
            }
            Text(item.title ?? "(no title)").lineLimit(2)
            if let p = item.price { Text(money(p)).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct ItemDetail: View {
    @EnvironmentObject var session: Session
    let sku: String
    @State private var item: Item?
    @State private var tote = ""
    @State private var notes = ""
    @State private var problem: String?
    @State private var done: String?

    var body: some View {
        Form {
            ProblemRow(text: problem)
            if let done { Text(done).foregroundStyle(.green) }
            if let item {
                Section {
                    Text(item.title ?? "(no title)").font(.headline)
                    LabeledContent("Status", value: label(item.status))
                    LabeledContent("Tote", value: item.tote ?? "unknown")
                    if let p = item.price { LabeledContent("Price", value: money(p)) }
                    if let q = item.quantity, q > 1 { LabeledContent("Units left", value: "\(q)") }
                    if let d = item.daysListed { LabeledContent("Days listed", value: "\(d)") }
                    if let s = item.suggestedPrice { LabeledContent("Suggested price", value: money(s)) }
                }
                Section("Move") {
                    HStack {
                        TextField("Tote, e.g. A5", text: $tote).textInputAutocapitalization(.characters).autocorrectionDisabled()
                        Button("Move") { act("move", ["tote": tote]) }.disabled(tote.isEmpty)
                    }
                }
                Section {
                    Button(item.missingSince == nil ? "Mark missing" : "Found it") { act("missing", ["missing": item.missingSince == nil]) }
                    Menu("Set status") {
                        ForEach(statuses, id: \.self) { s in Button(label(s)) { act("status", ["status": s]) } }
                    }
                }
                Section("Notes (our records only)") {
                    TextField("Notes", text: $notes, axis: .vertical)
                    Button("Save notes") { Task { await save() } }.disabled(notes == (item.notes ?? ""))
                }
            }
        }
        .navigationTitle(sku)
        .task { await load() }
    }

    func load() async {
        problem = await session.run {
            item = try await API.shared.get("/items/" + API.part(sku), as: ItemReply.self).item
            notes = item?.notes ?? ""
        }
    }

    func act(_ action: String, _ body: [String: Any]) {
        Task {
            problem = await session.run {
                struct R: Codable { let item: Item; let done: String }
                let r: R = try await API.shared.send("POST", "/items/\(API.part(sku))/\(action)", body)
                item = r.item; done = r.done; tote = ""
            }
        }
    }

    func save() async {
        problem = await session.run {
            item = try await API.shared.send("PATCH", "/items/" + API.part(sku), ["notes": notes], as: ItemReply.self).item
            done = "Notes saved."
        }
    }
}
