import SwiftUI

/// The website's home page: what needs attention, what's due to ship, today's work.
struct HomeView: View {
    @EnvironmentObject var session: Session
    @State private var home: HomeReply?
    @State private var problem: String?
    @State private var syncNote: String?

    var body: some View {
        NavigationStack {
            List {
                ProblemRow(text: problem)
                if let home {
                    Section("Needs attention") {
                        if home.attention.isEmpty { Text("Nothing. Nice.").foregroundStyle(.secondary) }
                        ForEach(home.attention) { a in
                            NavigationLink { AttentionList(attention: a) } label: {
                                HStack { Text("\(a.count)").bold().frame(minWidth: 40, alignment: .leading); Text(a.what) }
                            }
                        }
                    }
                    Section("Shipping") {
                        LabeledContent("Waiting to ship", value: "\(home.toShip)")
                        if !home.overdue.isEmpty { LabeledContent("Overdue", value: "\(home.overdue.count)").foregroundStyle(.red) }
                        LabeledContent("Due today", value: "\(home.dueToday.count)")
                        Button("Get new orders from eBay") { Task { await sync() } }
                        if let syncNote { Text(syncNote).font(.callout).foregroundStyle(.secondary) }
                        if let last = home.lastSync { Text("Last checked \(last.replacingOccurrences(of: "T", with: " ").prefix(16))").font(.caption).foregroundStyle(.secondary) }
                    }
                    if !home.today.isEmpty {
                        Section("Today") { ForEach(home.today, id: \.label) { LabeledContent($0.label, value: "\($0.count)") } }
                    }
                    Section("Items by status") {
                        ForEach(home.counts.sorted(by: { $0.key < $1.key }), id: \.key) { LabeledContent($0.key.replacingOccurrences(of: "_", with: " ").capitalized, value: "\($0.value)") }
                    }
                }
                Section { Button("Sign out \(session.user?.name ?? "")", role: .destructive) { session.signOut() } }
            }
            .navigationTitle("Home")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    func load() async { problem = await session.run { home = try await API.shared.get("/home") } }

    func sync() async {
        problem = await session.run {
            let r: SyncReply = try await API.shared.send("POST", "/sync/orders")
            syncNote = "\(r.orders) orders checked, \(r.newSales) new sales."
        }
        await load()
    }
}

/// An attention entry opens the matching list: items with its filters, or the pick list.
struct AttentionList: View {
    let attention: Attention
    var body: some View {
        switch attention.list {
        case "items": ItemList(title: attention.what.capitalized, filters: attention.filters.mapValues(\.text))
        case "picklist": PickListView()
        case "totes": TotesList(unchecked: true)
        default: Text("Open the \(attention.list) page on the website for this.").padding()
        }
    }
}
