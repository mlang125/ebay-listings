import PhotosUI
import SwiftUI

/// Start a tote, carry on with one still being shot, or draft finished ones with Claude.
struct CaptureHomeView: View {
    @EnvironmentObject var session: Session
    @ObservedObject private var uploads = UploadQueue.shared
    @State private var tote = ""
    @State private var nextFree = ""
    @State private var batches: [Batch] = []
    @State private var chosen: Set<Int> = []
    @State private var shooting: Batch?
    @State private var job: Job?
    @State private var estimate: String?
    @State private var problem: String?
    @State private var busy = false

    var open: [Batch] { batches.filter { $0.finishedAt == nil } }
    var toDraft: [Batch] { batches.filter { $0.finishedAt != nil && $0.items.contains { !$0.drafted && !$0.photos.isEmpty } } }
    var chosenCount: Int { toDraft.filter { chosen.contains($0.id) }.reduce(0) { $0 + $1.items.filter { !$0.drafted && !$0.photos.isEmpty }.count } }

    var body: some View {
        NavigationStack {
            List {
                ProblemRow(text: problem)
                ProblemRow(text: uploads.problem)
                Section("New tote") {
                    TextField(nextFree.isEmpty ? "Tote" : "Tote (next free: \(nextFree))", text: $tote)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Button(busy ? "Starting…" : "Start shooting \(tote.isEmpty ? nextFree : tote.uppercased())") { Task { await start() } }
                        .disabled(busy)
                }
                if !open.isEmpty {
                    Section("Still shooting") {
                        ForEach(open) { b in
                            Button { shooting = b } label: { BatchRow(batch: b) }.foregroundStyle(.primary)
                        }
                    }
                }
                if !toDraft.isEmpty {
                    Section {
                        ForEach(toDraft) { b in
                            Button { if chosen.contains(b.id) { chosen.remove(b.id) } else { chosen.insert(b.id) } } label: {
                                HStack {
                                    Image(systemName: chosen.contains(b.id) ? "checkmark.circle.fill" : "circle").font(.title2)
                                    BatchRow(batch: b)
                                }
                            }.foregroundStyle(.primary)
                        }
                        Button("CREATE DRAFTS (\(chosenCount) item\(chosenCount == 1 ? "" : "s"), about \(money(Double(chosenCount) * 0.10)))") {
                            Task { await createDrafts() }
                        }.bold().disabled(chosenCount == 0 || busy || (job.map { !$0.finished } ?? false))
                    } header: { Text("Finished, not drafted yet") } footer: {
                        Text("Claude writes each item's title, description, condition and price from its photos and note. Review them on the website's Drafts page.")
                    }
                }
                if let job { Section("Drafting") { JobRow(job: job, estimate: estimate) } }
            }
            .navigationTitle("Shoot")
            .refreshable { await load() }
            .task { await load() }
            .fullScreenCover(item: $shooting, onDismiss: { Task { await load() } }) { b in ShootView(batch: b) }
            .task(id: job.map { $0.finished ? -1 : $0.id }) { await follow() }
        }
    }

    func load() async {
        problem = await session.run {
            async let totes: TotesReply = API.shared.get("/totes")
            async let list: BatchesReply = API.shared.get("/batches")
            nextFree = try await totes.nextFree
            batches = try await list.batches
        }
    }

    func start() async {
        busy = true
        defer { busy = false }
        problem = await session.run {
            let r: BatchReply = try await API.shared.send("POST", "/batches", tote.isEmpty ? [:] : ["tote": tote])
            tote = ""
            shooting = r.batch
        }
    }

    func createDrafts() async {
        busy = true
        defer { busy = false }
        problem = await session.run {
            let r: JobStartReply = try await API.shared.send("POST", "/batches/drafts", ["batches": Array(chosen)])
            job = r.job
            estimate = "About \(money(r.estimateUsd))" + (r.noPhotos.isEmpty ? "" : ". Left out (no photos): \(r.noPhotos.joined(separator: ", "))")
            chosen = []
        }
    }

    /// While a job runs, check on it every few seconds.
    func follow() async {
        guard let current = job, !current.finished else { return }
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard let r = try? await API.shared.get("/jobs/\(current.id)", as: JobReply.self) else { continue }
            job = r.job
            if r.job.finished { await load(); return }
        }
    }
}

struct BatchRow: View {
    let batch: Batch
    var body: some View {
        HStack {
            Text(batch.tote ?? "?").bold().frame(width: 44, alignment: .leading)
            VStack(alignment: .leading) {
                Text("\(batch.items.count) item\(batch.items.count == 1 ? "" : "s"), \(batch.items.reduce(0) { $0 + $1.photos.count }) photos")
                Text(batch.name).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct JobRow: View {
    let job: Job
    let estimate: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ProgressView(value: Double(job.done), total: Double(max(job.total, 1)))
            Text(job.finished ? "\(job.status.capitalized): \(job.done - job.errors.count) of \(job.total) drafted, \(money(job.costUsd))"
                              : "Drafting \(job.done) of \(job.total)…")
            if job.status == "interrupted" { Text("The server restarted. Press Create drafts again to finish; drafted items are skipped.").font(.caption) }
            if let estimate, !job.finished { Text(estimate).font(.caption).foregroundStyle(.secondary) }
            ForEach(job.errors, id: \.self) { e in Text("\(e.sku ?? ""): \(e.error)").font(.caption).foregroundStyle(.red) }
        }
    }
}

/// Shooting a tote: the camera stays on; each page below it is one item. Swipe back to an earlier
/// item to add photos or fix its note, then swipe (or tap Latest) to carry on.
struct ShootView: View {
    @EnvironmentObject var session: Session
    @Environment(\.dismiss) private var dismiss
    @StateObject private var camera = Camera()
    @ObservedObject private var uploads = UploadQueue.shared
    @State var batch: Batch
    @State private var current: String?
    @State private var noteFor: CaptureItem?
    @State private var problem: String?
    @State private var flash = false
    @State private var confirmFinish = false
    @State private var picked: [PhotosPickerItem] = []

    var currentItem: CaptureItem? { batch.items.first { $0.sku == current } }
    var finished: Bool { batch.finishedAt != nil }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                if camera.available && !camera.denied { CameraPreview(session: camera.session) } else {
                    Color.black.overlay(Text(camera.denied ? "Camera access is off. Turn it on in Settings, or add photos from the library."
                                                           : "No camera here. Add photos from the library.")
                        .foregroundStyle(.white).multilineTextAlignment(.center).padding())
                }
                if flash { Color.white.opacity(0.6) }
                VStack {
                    HStack {
                        Button("Done") { dismiss() }.buttonStyle(.borderedProminent).tint(.black.opacity(0.6))
                        Spacer()
                        Text("Tote \(batch.tote ?? "?")").bold().padding(8).background(.black.opacity(0.6)).foregroundStyle(.white).clipShape(Capsule())
                        Spacer()
                        Button("Complete tote") { confirmFinish = true }.buttonStyle(.borderedProminent).tint(.green)
                            .disabled(finished || batch.items.isEmpty)
                    }.padding()
                    Spacer()
                    ProblemRow(text: problem ?? uploads.problem).padding(6).background(.black.opacity(0.6)).clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            .frame(maxHeight: .infinity)

            items.frame(height: 200)

            HStack(spacing: 24) {
                PhotosPicker(selection: $picked, maxSelectionCount: 20, matching: .images) {
                    Image(systemName: "photo.on.rectangle").font(.title2)
                }.disabled(current == nil || (currentItem?.drafted ?? true))
                Button { noteFor = currentItem } label: { Image(systemName: "asterisk.circle").font(.title) }
                    .disabled(currentItem == nil || (currentItem?.drafted ?? true))
                Button(action: shoot) {
                    Circle().stroke(.primary, lineWidth: 4).frame(width: 70, height: 70)
                        .overlay(Circle().fill(current == nil ? Color.gray : Color.primary).padding(6))
                }.disabled(current == nil || !camera.available || (currentItem?.drafted ?? true))
                Button { Task { await newItem() } } label: {
                    VStack { Image(systemName: "plus.rectangle.on.rectangle").font(.title2); Text("New item").font(.caption) }
                }.disabled(finished)
            }
            .padding(.vertical, 12)
        }
        .onAppear { camera.start(); current = batch.items.last?.sku }
        .onDisappear { camera.stop() }
        .onChange(of: uploads.uploaded) { Task { await reload() } }
        .onChange(of: picked) { Task { await addPicked() } }
        .sheet(item: $noteFor) { item in NoteSheet(batchId: batch.id, item: item) { updated in batch = updated } }
        .confirmationDialog("Complete tote \(batch.tote ?? "")?", isPresented: $confirmFinish, titleVisibility: .visible) {
            Button("Complete tote (\(batch.items.count) items)") { Task { await finish() } }
        } message: { Text("No new items after this, but you can still fix photos and notes until it's drafted.") }
    }

    var items: some View {
        VStack(spacing: 4) {
            if batch.items.isEmpty {
                Spacer()
                Text("Tap New item, then take its photos.").foregroundStyle(.secondary)
                Spacer()
            } else {
                TabView(selection: $current) {
                    ForEach(batch.items) { item in
                        ItemPage(batch: batch, item: item, number: item.position + 1, delete: { deleteItem(item) }, reload: reload)
                            .tag(Optional(item.sku))
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                HStack {
                    Text("Item \((batch.items.firstIndex { $0.sku == current } ?? 0) + 1) of \(batch.items.count)").font(.caption)
                    if current != batch.items.last?.sku {
                        Button("Latest ›") { withAnimation { current = batch.items.last?.sku } }.font(.caption.bold())
                    }
                }
            }
        }
        .background(Color(.secondarySystemBackground))
    }

    func shoot() {
        guard let sku = current else { return }
        withAnimation(.easeOut(duration: 0.1)) { flash = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { flash = false }
        camera.shoot { data in
            guard let data else { problem = "That photo didn't take; try again."; return }
            UploadQueue.shared.add(batchId: batch.id, sku: sku, jpeg: data)
        }
    }

    func addPicked() async {
        guard let sku = current, !picked.isEmpty else { return }
        for p in picked {
            if let data = try? await p.loadTransferable(type: Data.self), let jpeg = listingJPEG(data) {
                UploadQueue.shared.add(batchId: batch.id, sku: sku, jpeg: jpeg)
            }
        }
        picked = []
    }

    func newItem() async {
        problem = await session.run {
            let r: NewItemReply = try await API.shared.send("POST", "/batches/\(batch.id)/items")
            batch = r.batch
            withAnimation { current = r.sku }
        }
    }

    func deleteItem(_ item: CaptureItem) {
        Task {
            problem = await session.run {
                try await API.shared.sendNoReply("DELETE", "/batches/\(batch.id)/items/\(API.part(item.sku))")
                UploadQueue.shared.pending(for: item.sku).forEach { UploadQueue.shared.drop($0.clientId) }
                await reload()
                current = batch.items.last?.sku
            }
        }
    }

    func reload() async {
        if let r = try? await API.shared.get("/batches/\(batch.id)", as: BatchReply.self) { batch = r.batch }
    }

    func finish() async {
        problem = await session.run {
            batch = try await API.shared.send("POST", "/batches/\(batch.id)/finish", as: BatchReply.self).batch
        }
        if problem == nil { dismiss() }
    }
}

/// One item: its SKU (write it on the sticker), its photos so far, its note.
struct ItemPage: View {
    let batch: Batch
    let item: CaptureItem
    let number: Int
    let delete: () -> Void
    let reload: () async -> Void
    @ObservedObject private var uploads = UploadQueue.shared
    @EnvironmentObject var session: Session
    @State private var confirmDelete = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("#\(number)").foregroundStyle(.secondary)
                Text(item.sku).font(.title3.monospaced().bold())
                if item.drafted { Text("drafted").font(.caption).padding(4).background(.green.opacity(0.3)).clipShape(Capsule()) }
                Spacer()
                Menu { Button("Delete this item", role: .destructive) { confirmDelete = true } } label: { Image(systemName: "ellipsis.circle") }
                    .disabled(item.drafted)
            }
            ScrollView(.horizontal) {
                HStack(spacing: 6) {
                    ForEach(item.photos, id: \.file) { p in
                        AuthImage(path: p.url).frame(width: 84, height: 84).clipShape(RoundedRectangle(cornerRadius: 6))
                            .contextMenu { if !item.drafted { Button("Delete photo", role: .destructive) { Task { await deletePhoto(p) } } } }
                    }
                    ForEach(uploads.pending(for: item.sku)) { p in
                        ZStack(alignment: .bottomTrailing) {
                            if let img = uploads.image(p.clientId) { Image(uiImage: img).resizable().scaledToFill() } else { Color.gray }
                            Image(systemName: "arrow.up.circle.fill").foregroundStyle(.white).padding(3)
                        }
                        .frame(width: 84, height: 84).clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    if item.photos.isEmpty && uploads.pending(for: item.sku).isEmpty {
                        Text("No photos yet").foregroundStyle(.secondary).frame(height: 84)
                    }
                }
            }
            Text(noteSummary).font(.callout).lineLimit(2).foregroundStyle(noteSummary == "No note" ? .secondary : .primary)
        }
        .padding(.horizontal)
        .confirmationDialog("Delete \(item.sku) and its photos?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete item", role: .destructive, action: delete)
        }
    }

    var noteSummary: String {
        let parts = item.note.tags + (item.note.text.isEmpty ? [] : [item.note.text])
        return parts.isEmpty ? "No note" : parts.joined(separator: " · ")
    }

    func deletePhoto(_ p: Photo) async {
        let clientId = String(p.file.dropFirst(item.sku.count + 1).split(separator: ".")[0])
        _ = await session.run { try await API.shared.sendNoReply("DELETE", "/batches/\(batch.id)/items/\(API.part(item.sku))/photos/\(clientId)") }
        await reload()
    }
}

/// The note that replaces the whiteboard: quick tags (this person's usual ones first) and free text.
/// Claude reads both as written, so they can say the same thing or anything at all.
struct NoteSheet: View {
    @EnvironmentObject var session: Session
    @Environment(\.dismiss) private var dismiss
    let batchId: Int
    let item: CaptureItem
    let saved: (Batch) -> Void
    @State private var tags: [String] = []
    @State private var text = ""
    @State private var choices: [String] = []
    @State private var problem: String?
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            Form {
                ProblemRow(text: problem)
                Section("Quick tags") {
                    FlowLayout(spacing: 8) {
                        ForEach(choices, id: \.self) { tag in
                            Button(tag) { if let i = tags.firstIndex(of: tag) { tags.remove(at: i) } else { tags.append(tag) } }
                                .buttonStyle(.bordered).tint(tags.contains(tag) ? .accentColor : .gray)
                        }
                    }
                }
                Section("Note") {
                    TextField("Anything: what it is, what's wrong, what's missing", text: $text, axis: .vertical)
                        .lineLimit(3...8).focused($typing)
                }
            }
            .navigationTitle(item.sku)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } } }
            }
            .task {
                tags = item.note.tags
                text = item.note.text
                if let r = try? await API.shared.get("/note-tags", as: NoteTags.self) {
                    var seen = Set<String>()
                    choices = (r.recent + r.standard + item.note.tags).filter { seen.insert($0).inserted }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    func save() async {
        problem = await session.run {
            let r: BatchReply = try await API.shared.send("PATCH", "/batches/\(batchId)/items/\(API.part(item.sku))",
                                                          ["note": ["tags": tags, "text": text]])
            saved(r.batch)
            dismiss()
        }
    }
}

/// Lays tag buttons out in rows that wrap.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(proposal.width ?? 300, subviews)
        return CGSize(width: proposal.width ?? 300, height: rows.last.map { $0.y + $0.height } ?? 0)
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var i = 0
        for row in arrange(bounds.width, subviews) {
            var x = bounds.minX
            for _ in 0..<row.count {
                let size = subviews[i].sizeThatFits(.unspecified)
                subviews[i].place(at: CGPoint(x: x, y: bounds.minY + row.y), proposal: .unspecified)
                x += size.width + spacing
                i += 1
            }
        }
    }
    private func arrange(_ width: CGFloat, _ subviews: Subviews) -> [(count: Int, y: CGFloat, height: CGFloat)] {
        var rows: [(count: Int, y: CGFloat, height: CGFloat)] = []
        var x: CGFloat = 0, y: CGFloat = 0, count = 0, height: CGFloat = 0
        for v in subviews {
            let size = v.sizeThatFits(.unspecified)
            if count > 0 && x + size.width > width {
                rows.append((count, y, height)); y += height + spacing; x = 0; count = 0; height = 0
            }
            x += size.width + spacing; count += 1; height = max(height, size.height)
        }
        if count > 0 { rows.append((count, y, height)) }
        return rows
    }
}
