import SwiftUI

struct ShipView: View {
    var body: some View { NavigationStack { PickListView() } }
}

/// Sold items waiting to ship, by tote. Tap one to move it to the next step.
struct PickListView: View {
    @EnvironmentObject var session: Session
    @State private var items: [PickItem] = []
    @State private var problem: String?
    @State private var chosen: PickItem?

    let steps = [("LABEL_PRINTED", "Label printed"), ("PACKAGED", "Packed"), ("SENT_TO_POST_OFFICE", "Dropped off")]

    var body: some View {
        List {
            ProblemRow(text: problem)
            if items.isEmpty && problem == nil { Text("Nothing waiting to ship.").foregroundStyle(.secondary) }
            ForEach(Dictionary(grouping: items, by: { $0.tote ?? "?" }).sorted(by: { $0.key < $1.key }), id: \.key) { tote, rows in
                Section(tote) {
                    ForEach(rows) { r in
                        Button { chosen = r } label: {
                            VStack(alignment: .leading) {
                                HStack {
                                    Text(r.sku).font(.caption.monospaced())
                                    Text(label(r.status)).font(.caption).foregroundStyle(.secondary)
                                    if r.missingSince != nil { Text("missing").font(.caption).foregroundStyle(.red) }
                                    Spacer()
                                    if let by = r.shipBy { Text("by \(by)").font(.caption) }
                                }
                                Text(r.title ?? "").lineLimit(2)
                            }
                        }.foregroundStyle(.primary)
                    }
                }
            }
        }
        .navigationTitle("Pick list")
        .confirmationDialog(chosen?.title ?? "", isPresented: Binding(get: { chosen != nil }, set: { if !$0 { chosen = nil } }), titleVisibility: .visible) {
            ForEach(steps, id: \.0) { step, name in
                Button(name) { if let c = chosen { Task { await pack(c.sku, step) } } }
            }
        }
        .refreshable { await load() }
        .task { await load() }
    }

    func load() async { problem = await session.run { items = try await API.shared.get("/picklist", as: PickListReply.self).items } }

    func pack(_ sku: String, _ step: String) async {
        problem = await session.run { _ = try await API.shared.send("POST", "/pack", ["sku": sku, "step": step], as: PackReply.self) }
        await load()
    }
}
