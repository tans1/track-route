/* The route and fuel plan are embedded snapshots. This page never requests routing. */
(() => {
  "use strict";
  const status = document.getElementById("map-status");
  const showStatus = (message) => {
    status.textContent = message;
    status.hidden = false;
  };
  if (!window.L) {
    showStatus("The map could not load. Your route summary and fuel stops are still available.");
    document.getElementById("fit-route").disabled = true;
    return;
  }

  try {
    const plan = JSON.parse(document.getElementById("trip-plan").textContent);
    const tileURL = JSON.parse(document.getElementById("tile-url").textContent);
    const tileAttribution = JSON.parse(document.getElementById("tile-attribution").textContent);
    const map = L.map("route-map", {scrollWheelZoom: false, zoomControl: false});
    L.control.zoom({position: "topright"}).addTo(map);
    const tiles = L.tileLayer(tileURL, {
      maxZoom: 19,
      attribution: tileAttribution,
      keepBuffer: 0,
      updateWhenIdle: true,
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(map);
    tiles.on("tileerror", () => showStatus("Base-map tiles are unavailable. The route and fuel stops are still shown."));

    const route = L.geoJSON(plan.route, {
      style: {color: "#17694f", weight: 4, opacity: 0.9, smoothFactor: 0},
    }).addTo(map);
    const access = L.geoJSON(plan.estimated_access, {
      style: {color: "#bc7623", weight: 3, dashArray: "5 6", opacity: 0.85, smoothFactor: 0},
    }).addTo(map);

    const paragraph = (parent, text, className) => {
      const node = document.createElement("p");
      node.textContent = text;
      if (className) node.className = className;
      parent.appendChild(node);
    };
    const popup = (title, lines, note) => {
      const content = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = title;
      content.appendChild(heading);
      lines.forEach((line) => paragraph(content, line));
      if (note) paragraph(content, note, "popup-note");
      return content;
    };
    const icon = (label, className = "") => {
      const node = document.createElement("span");
      node.textContent = String(label);
      return L.divIcon({
        html: node,
        className: `route-marker ${className}`,
        iconSize: [30, 30],
        iconAnchor: [15, 15],
        popupAnchor: [0, -14],
      });
    };
    const latLng = (coordinates) => [coordinates[1], coordinates[0]];
    const bounds = route.getBounds();
    const markers = new Map();
    for (const stop of plan.stops) {
      const label = stop.name || stop.station_id;
      const place = [stop.address, stop.city, stop.state].filter(Boolean).join(", ");
      const quality = stop.coordinate_quality === "representative_place_internal_point"
        ? "Approximate city location; this point may not be the station entrance."
        : "Station access and detour distances are estimated.";
      const marker = L.marker(latLng(stop.coordinates), {
        icon: icon(stop.order),
        title: `Stop ${stop.order}: ${label}`,
        alt: `Fuel stop ${stop.order}`,
        riseOnHover: true,
      }).addTo(map).bindPopup(popup(
        `${stop.order}. ${label}`,
        [place, `${Number(stop.gallons_purchased).toFixed(2)} gal at $${stop.price_per_gallon}/gal`,
          `Purchase: $${stop.purchase_cost}`, `Fuel on arrival: ${Number(stop.arrival_fuel_gallons).toFixed(2)} gal`],
        quality,
      ));
      markers.set(stop.order, marker);
      bounds.extend(marker.getLatLng());
    }
    if (access.getBounds().isValid()) bounds.extend(access.getBounds());
    const coordinates = plan.route.features[0].geometry.coordinates;
    const endpoints = [
      ["S", coordinates[0], plan.start, "endpoint", "Start"],
      ["D", coordinates[coordinates.length - 1], plan.destination, "endpoint destination", "Destination"],
    ];
    endpoints.forEach(([label, point, location, className, role]) => {
      L.marker(latLng(point), {icon: icon(label, className), title: `${role}: ${location.query}`, zIndexOffset: -100})
        .addTo(map).bindPopup(popup(role, [location.display_name || location.query]));
    });
    const fitRoute = () => {
      map.closePopup();
      map.fitBounds(bounds, {padding: [36, 44], maxZoom: 13, animate: false});
    };
    document.getElementById("fit-route").addEventListener("click", fitRoute);
    document.querySelectorAll("[data-stop-order]").forEach((button) => {
      button.addEventListener("click", () => {
        const marker = markers.get(Number(button.dataset.stopOrder));
        if (!marker) return;
        document.querySelectorAll("[data-stop-order]").forEach((other) => other.removeAttribute("aria-current"));
        button.setAttribute("aria-current", "true");
        map.setView(marker.getLatLng(), 13, {animate: false});
        marker.openPopup();
        if (window.matchMedia("(max-width: 850px)").matches) {
          document.getElementById("route-map").scrollIntoView({block: "center", behavior: "auto"});
        }
      });
    });
    fitRoute();
    status.hidden = true;
    document.getElementById("route-map").dataset.ready = "true";
  } catch (error) {
    showStatus("The map could not be displayed. Your fuel plan is still available in the trip summary.");
    document.getElementById("fit-route").disabled = true;
  }
})();
