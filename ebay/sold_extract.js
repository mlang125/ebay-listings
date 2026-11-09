// Run on an eBay sold-listings search page (comps.sold_url). Returns the sold
// results above eBay's "Results matching fewer words" divider as JSON for
// comps.record_sold(): price, shipping, date, condition, how it sold, title, url.
(() => {
  const months = {Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12};
  const money = (s) => (s ? parseFloat(s.replace(/[^0-9.]/g, "")) : null);
  const results = [];
  const items = document.querySelectorAll("li.s-item, li.s-card, li.srp-river-answer");
  for (const el of items) {
    // eBay inserts this divider before loosely matching results; stop there.
    if (/results matching fewer words/i.test(el.innerText)) break;
    const link = el.querySelector('a[href*="/itm/"]');
    if (!link) continue;
    const lines = el.innerText.split("\n").map((l) => l.trim()).filter(Boolean);
    const soldLine = lines.find((l) => /^Sold\s+\w{3}\s+\d{1,2},\s+\d{4}/.test(l));
    if (!soldLine) continue;
    const [, mon, day, year] = soldLine.match(/^Sold\s+(\w{3})\s+(\d{1,2}),\s+(\d{4})/);
    const priceLine = lines.find((l) => /^\$[\d,]+\.\d{2}/.test(l));
    const shipLine = lines.find((l) => /(shipping|delivery)/i.test(l) && /\$|free/i.test(l));
    const title = lines[lines.indexOf(soldLine) + 1] || "";
    results.push({
      price: money(priceLine),
      shipping: shipLine ? (/free/i.test(shipLine) ? 0 : money(shipLine.match(/\$[\d,.]+/)?.[0])) : null,
      date: `${year}-${String(months[mon]).padStart(2, "0")}-${day.padStart(2, "0")}`,
      condition: lines.find((l) => /^(Pre-Owned|Brand New|New|Open Box|Used|Parts Only|Refurbished)/i.test(l)) || null,
      sold_via: lines.find((l) => /bids?$|Best offer accepted|Buy It Now|or Best Offer/i.test(l)) || null,
      title: title.replace(/^New Listing/i, "").trim(),
      url: link.href.split("?")[0],
    });
  }
  return results;
})();
