import SwiftUI

struct TotesView: View {
    var body: some View { NavigationStack { TotesList() } }
}

struct TotesList: View {
    @EnvironmentObject var session: Session
    var unchecked = false
    @State private var totes: [Tote] = []
    @State private var nextFree = ""
    @State private var problem: String?

    var body: some View {
            List {
                ProblemRow(text: problem)
                if !nextFree.isEmpty { Text("Next free tote: \(nextFree)").foregroundStyle(.secondary) }
                ForEach(totes.filter { !unchecked || ($0.kind == "tote" && $0.checkedAt == nil) }) { t in
                    NavigationLink { ToteDetail(code: t.code) } label: {
                        HStack {
                            Text(t.code).bold().frame(width: 50, alignment: .leading)
                            Text("\(t.onHand ?? 0) on hand").foregroundStyle(.secondary)
                            if (t.missing ?? 0) > 0 { Text("\(t.missing ?? 0) missing").foregroundStyle(.red) }
                            Spacer()
                            if t.checkedAt == nil && t.kind == "tote" { Text("never checked").font(.caption).foregroundStyle(.orange) }
                        }
                    }
                }
            }
            .navigationTitle(unchecked ? "Never checked" : "Totes")
            .refreshable { await load() }
            .task { await load() }
    }

    func load() async {
        problem = await session.run {
            let r: TotesReply = try await API.shared.get("/totes?sort=code")
            totes = r.totes; nextFree = r.nextFree
        }
    }
}

struct ToteDetail: View {
    @EnvironmentObject var session: Session
    let code: String
    @State private var items: [Item] = []
    @State private var problem: String?
    @State private var done: String?

    var onHand: [Item] { items.filter { !["SENT_TO_POST_OFFICE", "DELIVERED"].contains($0.status) } }

    var body: some View {
        List {
            ProblemRow(text: problem)
            if let done { Text(done).foregroundStyle(.green) }
            Section {
                NavigationLink("Count this tote") { CountView(code: code, items: onHand) }
                Button("Mark checked") {
                    Task { problem = await session.run { try await API.shared.sendNoReply("POST", "/totes/\(API.part(code))/checked"); done = "Recorded that \(code) was checked." } }
                }
            }
            Section("\(onHand.count) on hand") {
                ForEach(onHand) { item in NavigationLink { ItemDetail(sku: item.sku) } label: { ItemRow(item: item) } }
            }
        }
        .navigationTitle(code)
        .refreshable { await load() }
        .task { await load() }
    }

    func load() async { problem = await session.run { items = try await API.shared.get("/totes/" + API.part(code), as: ToteReply.self).items } }
}

/// Go through a tote: tick what's there. Unticked items become missing; SKUs typed in move here.
struct CountView: View {
    @EnvironmentObject var session: Session
    @Environment(\.dismiss) private var dismiss
    let code: String
    let items: [Item]
    @State private var here: Set<String> = []
    @State private var extra = ""
    @State private var problem: String?
    @State private var result: String?

    var body: some View {
        List {
            ProblemRow(text: problem)
            if let result { Text(result).foregroundStyle(.green) }
            Section("Tick what's in \(code)") {
                ForEach(items) { item in
                    Button { if here.contains(item.sku) { here.remove(item.sku) } else { here.insert(item.sku) } } label: {
                        HStack {
                            Image(systemName: here.contains(item.sku) ? "checkmark.circle.fill" : "circle").font(.title2)
                            ItemRow(item: item)
                        }
                    }.foregroundStyle(.primary)
                }
            }
            Section("Also found here (SKUs)") {
                TextField("e.g. 920 931", text: $extra).textInputAutocapitalization(.characters).autocorrectionDisabled()
            }
            Section {
                Button("Save count: \(here.count) here, \(items.count - here.count) missing") { Task { await save() } }
            }
        }
        .navigationTitle("Count \(code)")
    }

    func save() async {
        let extras = extra.split(whereSeparator: { $0 == " " || $0 == "," }).map(String.init)
        problem = await session.run {
            let r: CountReply = try await API.shared.send("POST", "/totes/\(API.part(code))/count", ["here": Array(here), "extra": extras])
            result = r.message
        }
    }
}
