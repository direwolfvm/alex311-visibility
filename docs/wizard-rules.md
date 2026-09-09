# Wizard rules — discovered per category

*Generated 2026-09-09 21:57 UTC by `spike/wizard_rules.py`, read-only (it never presses Submit). Each option of each list question was selected in place and its effect recorded. Skipped because not in the current catalog: Snow and Ice Removal Sidewalk.*

**Hard stop** — a Validation Alert appeared and the answer was rejected after OK. **Advisory** — an alert appeared but the answer stood. **Info** — an inline message without a modal. **Reveals** — which question numbers became visible on choosing that option; options that reveal different sets are skip-logic the form must replicate.

| Category | Code | Questions | Hard stops | Advisory | Info msgs | Branching | Continue at end |
|---|---|---|---|---|---|---|---|
| Missed Collection | `TESMISCO` | 2 | 0 | 0 | 0 | no | True |
| Trash and Recycling Containers | `TESCONTN` | 3 | 0 | 4 | 1 | yes | True |
| Metal or Appliance Collection | `TESMETAL` | 1 | 0 | 0 | 0 | no | True |
| Traffic Light/Signal (Existing) | `TESTSGNLM` | 3 | 0 | 2 | 1 | yes | True |
| Parking Comments, Complaints or Inquiries | `ALXPARK` | 2 | 3 | 3 | 0 | yes | True |
| Potholes | `TESPOTHO` | 2 | 0 | 0 | 0 | no | True |
| Traffic Sign | `TESTSIGN` | 2 | 0 | 0 | 0 | yes | True |
| Park Maintenance | `RPCMAIN` | 2 | 0 | 0 | 7 | no | True |
| Noise Issues | `TESNOISE` | 3 | 1 | 0 | 0 | no | True |
| Sewer Infrastructure Issues | `TESSEWER` | 2 | 1 | 3 | 3 | yes | True |
| Cleaning and Sweeping (Alley and Street) | `TESCLNSW` | 3 | 0 | 0 | 0 | no | True |
| Streetlights (Existing) | `TESSTLGHT` | 3 | 0 | 0 | 0 | no | True |
| Sidewalk | `TESSIDEWALK` | 3 | 2 | 0 | 0 | no | True |
| Tree Inspection Request | `RPCATREINSP` | 5 | 3 | 1 | 0 | yes | True |
| Bulk Yard Waste Pickup | `TESYRDWS` | 4 | 4 | 0 | 0 | yes | True |

## Missed Collection (`TESMISCO`)

Banners: “Please follow directions for any tag that has been placed on your items. And put out your items for your next collection day.” · “Please provide the details of your request in the Additional Information box below.”

1. **Which items were missed?** — `01PL-MISSEDTYP` — radio; options: Trash, Recycling, Yard Waste
2. **When were your items set out for pickup?** — composite: MM/DD/YYYY, 12:00, select

## Trash and Recycling Containers (`TESCONTN`)

Banners: “The City is not currently providing yard waste containers.” · “Please provide the details of your request in the Additional Information box below.”

1. **What is the nature of your container request?** *(required)* — select; options: New Resident, New Commercial Refuse Customer, Missing Container, Damaged Container, Exchange for Different Recycling Container Size, Request Additional Container, Remove Container
   - New Resident: advisory; “Please check to see if your home already has containers before submitting this request.”; reveals → Q2, Q3
   - New Commercial Refuse Customer: reveals → Q2, Q3
   - Missing Container: advisory; “Please check the surrounding area before requesting a new container”; reveals → Q2
   - Damaged Container: reveals → Q2, Q3
   - Exchange for Different Recycling Container Size: advisory; “Please clearly mark the container and make sure it is accessible on your next collection day”; reveals → Q2, Q3
   - Request Additional Container: reveals → Q2
   - Remove Container: advisory; “Please clearly mark the container and make sure it is accessible on your next collection day”; reveals → Q2, Q3
2. **What type of container?** *(required)* — `01PL-CONTYPE` — radio; options: Trash, Recycling
   - Trash: “Trash containers are available in one size only.”
   - Recycling: reveals → Q4
3. **Date of move in?** *(required)* — composite: MM/DD/YYYY, 12:00, select

## Metal or Appliance Collection (`TESMETAL`)

Banners: “Collection only occurs curbside, no alley collection. Please make sure your items are placed out by 6:00 a.m. the first Tuesday or Wednesday after your request was submitted.” · “Please provide the details of your request in the Additional Information box below.”

1. **What type of item are you requesting for collection?** *(required)* — checkbox; options: Appliance (Stove/Refrigerator/Washer/ Dryer/Freezer/Hot Water Heater), Metal (Fencing/Bicycles)

## Traffic Light/Signal (Existing) (`TESTSGNLM`)

1. **What type of signal is it?** *(required)* — `01PL-TSGNLTYP` — radio; options: Traffic Signal, Pedestrian Signal, Other
   - Traffic Signal: reveals → Q2, Q3
   - Pedestrian Signal: reveals → Q2, Q3
   - Other: reveals → Q2, Q3, Q4
2. **What is the traffic signal problem?** *(required)* — select; options: Bulb out, Twisted, Timing Concern, Change Needed (Please be aware that request for changes require a detailed review by staff for approval), Intersection Dark (Emergency), Flashing (Emergency), Other
   - Change Needed (Please be aware that request for changes require a detailed review by staff for approval): “Please describe in detail the modification needed in the Additional Information box below. Pictures are helpful for staff to properly address your concern.”
   - Intersection Dark (Emergency): advisory; “Please contact (703) 746-4444 after hours.”
   - Flashing (Emergency): advisory; “Please contact (703) 746-4444 after hours.”
3. **Approximately when was the problem observed?** *(required)* — checkbox; options: Morning, Afternoon, Evening, Overnight

## Parking Comments, Complaints or Inquiries (`ALXPARK`)

Banners: “Please provide the details of your request in the Additional Information box below.”

1. **What is the nature of your request?** *(required)* — select; options: Contest Parking Citation, Parking Enforcement, Temporary Reserved Parking, Residential Parking Permit Program, Modification to Parking Regulations, General Inquiries
   - Contest Parking Citation: **HARD STOP**; “Please go to the City's Adjudication Office to contest Parking Tickets: Contest Parking Tickets”
   - Parking Enforcement: reveals → Q2
   - Temporary Reserved Parking: advisory; “To reserve parking for a moving van, funeral, wedding, or other event, please apply for a Temporary Reserved Parking permit”
   - Residential Parking Permit Program: advisory; “To learn more about adding residential permit parking restrictions to your block, please visit Parking Restrictions and Districts Overview for information about the petition process that can be started by residents.”
   - Modification to Parking Regulations: **HARD STOP**; “To request other changes to parking restrictions (no parking areas, loading zones, etc.), please complete the On-Street Parking Modification Request Form.”
   - General Inquiries: advisory; “For information on Parking in Alexandria, visit www.alexandriava.gov/Parking.”
2. **Is this parking enforcement concern occurring right now?** *(required)* — `01PL-OCCURNOW` — radio; options: Yes, No
   - Yes: **HARD STOP**; “Please call 703.746.4444 to report a parking concern occurring right now.”

## Potholes (`TESPOTHO`)

Banners: “Please provide additional location information in the box below. Landmarks, travel lanes, and intersections are always helpful.”

1. **Where is the pothole located?** *(required)* — `01PL-WHRPOTHLOC` — radio; options: Alley, Street
2. **What is the surface type?** — `01PL-WHTSURFTYP` — radio; options: Asphalt, Concrete, Other

## Traffic Sign (`TESTSIGN`)

Banners: “Please provide the details of your request in the Additional Information box below.”

1. **What is the nature of your traffic sign request?** *(required)* — `01PL-TRSGNLTYPE` — radio; options: Damaged or Down Sign, Report Faded Sign, Request New Sign, Visibility of Sign Obstructed
   - Damaged or Down Sign: reveals → Q2
   - Report Faded Sign: reveals → Q2
   - Request New Sign: reveals → Q2
   - Visibility of Sign Obstructed: reveals → Q2
2. **What type of traffic sign pertains to your request?** *(required)* — `01PL-SIGNTYPE` — radio; options: Stop Sign, Street Name Sign, Parking Sign, Other
   - Other: reveals → Q3

## Park Maintenance (`RPCMAIN`)

1. **Please indicate which park your request pertains to** *(required)* — text: Provide information here
2. **What is the nature of your request?** *(required)* — select; options: Mowing, Litter, Playgrounds, Dog Parks, Trail, Fence or Bench Repair, Other
   - Mowing: “Please provide the details of your request in the Additional Information box below.”
   - Litter: “Please provide the details of your request in the Additional Information box below.”
   - Playgrounds: “Please provide the details of your request in the Additional Information box below.”
   - Dog Parks: “Please provide the details of your request in the Additional Information box below.”
   - Trail: “Please provide the details of your request in the Additional Information box below.”
   - Fence or Bench Repair: “Please provide the details of your request in the Additional Information box below.”
   - Other: “In the box below please describe the specifics of your request and provide detailed information regarding where the problem is located. Pictures and landmarks are helpful.”

## Noise Issues (`TESNOISE`)

Banners: “If you are submitting this request between 7pm and 7am, or over the weekend, please call 703.746.4444 for immediate assistance.” · “Aircraft are exempt from the City Noise Code. Complaints can be logged with the Metropolitan Washington Airport Authority complaint line by calling 703.417.1204.” · “Please provide as much additional detail as possible on the nature of the noise complaint in the box below.”

1. **What is the source of the noise as best you can determine?** — `01PL-NOISESOUR` — radio; options: Residential, Commercial, Other
2. **Please select the type of noise issue:** *(required)* — `01IN-NOISETYP` — radio; options: Construction, Animal, Other
   - ⚠ rendered but never present in the mined data: Animal
   - Animal: **HARD STOP**; “Please contact the Animal Welfare League at 703-746-4774.”
3. **When did the noise occur?** — composite: MM/DD/YYYY, 12:00, select

## Sewer Infrastructure Issues (`TESSEWER`)

1. **What is the nature of your sewer request?** *(required)* — select; options: Backup - Not Weather Related, Flooding, Cave-in or Sinkhole, Missing or Broken Manhole Cover, Odor, Storm Drain, Sump Pump & Down Spout Issues, Watermain Break, Other
   - Backup - Not Weather Related: reveals → Q2
   - Flooding: reveals → Q2
   - Cave-in or Sinkhole: advisory; “STOP, before continuing the submission please contact 703-746-4444 to report this safety concern. / Please provide the details of your request in the Additional Information box below.”; reveals → Q2
   - Missing or Broken Manhole Cover: advisory; “STOP, before continuing the submission please contact 703-746-4444 to report this safety concern. / Please provide the details of your request in the Additional Information box below.”; reveals → Q2
   - Odor: “Please provide the details of your request in the Additional Information box below.”; reveals → Q2
   - Sump Pump & Down Spout Issues: “Please provide the details of your request in the Additional Information box below.”; reveals → Q2
   - Watermain Break: advisory; “Please call American Water at 703.549.7080 / Please provide the details of your request in the Additional Information box below.”; reveals → Q2
   - Other: “Please provide the details of your request in the Additional Information box below.”; reveals → Q2
2. **Is the issue inside your house?** — `01PL-FLOODING` — radio; options: Yes, No
   - Yes: **HARD STOP**; “Please contact a plumber for assistance prior to moving forward with your request.”
   - No: reveals → Q3

## Cleaning and Sweeping (Alley and Street) (`TESCLNSW`)

Banners: “Downtown King Street is cleaned on a regular basis by the City. See the City's Street Sweeping webpage for more details.” · “Please provide the details of your request in the Additional Information box below. Pictures are helpful for staff to properly locate your issue.”

1. **Where are you requesting this service?** *(required)* — `01PL-CLNSWPLOC` — radio; options: Alley, Street
2. **Are you requesting?** — `01PL-ALLEYINFO` — radio; options: Sweeping or cleaning, Vegetation control
3. **What type of debris?** — `01PL-DEBRIS` — radio; options: Gravel, Mud, Glass, Leaves, Other

## Streetlights (Existing) (`TESSTLGHT`)

Banners: “Please provide the details of your request in the Additional Information box below. Pictures are helpful for staff to properly locate your issue.”

1. **What type of light are you reporting?** *(required)* — `01PL-LIGHTTYPE` — radio; options: Street, Park or Trail Light, Tunnel Light, Traffic Light/Signal
2. **What is the problem?** *(required)* — select; options: Light out, Stays on, Flickering, Wire Down, Pole Down, Shining too Bright, Other - Please add details in Additional Information box
3. **Please provide any other identification found on the pole (if applicable).** — text: Provide information here

## Sidewalk (`TESSIDEWALK`)

Banners: “If this is a major hazard please call 703.746.4444 to report” · “Pictures are helpful for staff to properly locate your issue.” · “Please provide additional location information in the box below. Landmarks, travel lanes, and intersections are always helpful.”

1. **Is this request for a sidewalk that does not currently exist?** *(required)* — `01PL-IFWANTNEW` — radio; options: Yes, No
   - ⚠ rendered but never present in the mined data: Yes
   - Yes: **HARD STOP**
2. **What is the surface type?** — select; options: Asphalt, Brick, Concrete, Cobblestone, Gravel, Unknown
3. **Is the damaged portion of the sidewalk part of a driveway apron?** *(required)* — `01PL-DRWAYAPRN` — radio; options: Yes, No, Unsure
   - Yes: **HARD STOP**; “This is the responsibility of the homeowner or HOA. Please contact 311, 703.746.4311 or TES Permitting for more information TESPermits@alexandriava.gov”

## Tree Inspection Request (`RPCATREINSP`)

Banners: “If this is an immediate danger please call 911.” · “Please provide the details of your request in the Additional Information box below.”

1. **Is the tree blocking the road?** *(required)* — `01PL-RPCTREERD` — radio; options: Yes, No
   - Yes: **HARD STOP**; “Please call the Police non-emergency line at 703.746.4444 to report.”
   - No: reveals → Q2
2. **Are the tree roots damaging the sidewalk?** *(required)* — `01PL-RPCTREESIDE` — radio; options: Yes, No
   - ⚠ rendered but never present in the mined data: Yes
   - Yes: **HARD STOP**; “Please use suggested service type.”
3. **Is the tree on public or private property?** *(required)* — `01PL-TREEPROPERTY` — radio; options: Private, Public
   - ⚠ rendered but never present in the mined data: Private
   - Private: **HARD STOP**
   - Public: reveals → Q4
4. **What is the nature of your request?** *(required)* — `01PL-RPCTREINSP` — radio; options: Tree Pruning, Tree Removal
   - Tree Removal: advisory; “A City Arborist will have to inspect the site and determine the appropriate next steps”; reveals → Q5
5. **Please describe why the service is needed.** *(required)* — text: Provide information here

## Bulk Yard Waste Pickup (`TESYRDWS`)

Banners: “Please provide the details of your request in the Additional Information box below.”

1. **Have you reviewed the City’s guidelines for bulk yard waste pickup?** *(required)* — `01PL-BULKGUIDE` — radio; options: Yes, No
   - Yes: reveals → Q2
   - No: **HARD STOP**; “Please review the City's guidelines for bulk yard waste pick up on our website at: www.alexandriava.gov/YardWaste”
2. **Are the materials in paper yard waste bags or in a reusable container weighing less than 45 lbs?** *(required)* — `01PL-BULKWEIGHT` — radio; options: Yes, No
   - ⚠ rendered but never present in the mined data: Yes
   - Yes: **HARD STOP**; “A 311 request is not required for materials in paper yard waste bags or in reusable containers that are less than 45 lbs. Please set this material out with your regular weekly yard waste collection.”
   - No: reveals → Q3
3. **Have the materials been placed curbside?** *(required)* — `01PL-YRDWSLOC` — radio; options: Yes, No
   - ⚠ rendered but never present in the mined data: No
   - Yes: reveals → Q4
   - No: **HARD STOP**; “Please refer to the City's guidelines for bulk yard waste pickup at www.alexandriava.gov/YardWaste and set the materials at your collection point.”
4. **Do the materials include tree stumps?** *(required)* — `01PL-BULKTREE` — radio; options: Yes, No
   - ⚠ rendered but never present in the mined data: Yes
   - Yes: **HARD STOP**; “The City does not collect tree stumps. Please contact a private hauler to have them removed.”
